"""Completion notifications: detecting the transition to 100 %, exactly once, across restarts."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from graph_client import GraphThrottled
from healthcheck import is_healthy
from helpers import TENANT_ID, async_test, make_world, restarted
from notifier import CompletionEvent, CompletionNotifier, DeliveryError, task_state_from_graph
from state import StateStore


class Channel:
    """Stands in for the Discord channel: remembers what was delivered, can be made to fail."""

    def __init__(self) -> None:
        self.events: list[CompletionEvent] = []
        self.error: Exception | None = None
        self.attempts = 0

    async def deliver(self, event: CompletionEvent) -> None:
        self.attempts += 1
        if self.error is not None:
            raise self.error
        self.events.append(event)

    @property
    def titles(self) -> list[str]:
        return [event.title for event in self.events]


def start(world, tmp_path, channel=None, **options):
    """Open the state database and build a notifier, as the bot does at start-up."""
    channel = channel or Channel()
    store = StateStore(tmp_path / "state.sqlite3")
    store.open()
    notifier = CompletionNotifier(
        planner=world.planner,
        store=store,
        deliver=channel.deliver,
        now=lambda: world.graph.now,
        heartbeat_path=tmp_path / "heartbeat",
        **options,
    )
    return store, notifier, channel


async def cycle(world, notifier) -> None:
    """One minute passes, then the bot polls and sends."""
    world.graph.tick(60)
    await notifier.poll_once()
    await notifier.flush()


@async_test
async def test_tasks_already_complete_at_start_are_not_announced(tmp_path, caplog):
    world = make_world()
    world.graph.add_task(world.plan_id, "Old and done", percent=100)
    world.graph.add_task(world.plan_id, "Still open")
    store, notifier, channel = start(world, tmp_path)

    with caplog.at_level(logging.INFO):
        await cycle(world, notifier)
        await cycle(world, notifier)

    assert channel.events == []
    assert store.counts_sync() == {"tasks": 2}
    assert any(
        "Now watching plan “Sprint Board”: 2 tasks, 1 already complete" in r.getMessage() for r in caplog.records
    )


@async_test
async def test_completing_a_task_is_announced_once_with_all_details(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs", bucket_id=world.doing)
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)

    completed_at = world.graph.tick(30)
    world.graph.complete(task_id, by=world.anna)
    await cycle(world, notifier)

    assert len(channel.events) == 1
    event = channel.events[0]
    assert event.title == "Write docs"
    assert event.completed_by == "Anna Kowalska"
    assert event.bucket == "In progress"
    assert event.plan_title == "Sprint Board"
    assert (
        event.url
        == f"https://planner.cloud.microsoft/webui/plan/{world.plan_id}/view/board/task/{task_id}?tid={TENANT_ID}"
    )
    assert event.completed_at == completed_at
    assert event.discord_user_id is None

    for _ in range(3):
        await cycle(world, notifier)
    assert len(channel.events) == 1
    assert store.counts_sync() == {"tasks": 1, "sent": 1}


@async_test
async def test_only_the_step_to_100_percent_counts(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)

    world.graph.set_progress(task_id, 50)
    await cycle(world, notifier)
    assert channel.events == []

    world.graph.complete(task_id, by=world.jan)
    await cycle(world, notifier)
    assert channel.titles == ["Write docs"]

    world.graph.touch(task_id)  # edited after completion: the ETag changes, the state doesn't
    await cycle(world, notifier)
    assert channel.titles == ["Write docs"]


@async_test
async def test_edits_to_a_completed_task_without_a_completion_time_are_not_announced_again(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)

    # Should Planner ever leave completedDateTime empty, the saved state alone must
    # still tell "became complete" from "was already complete".
    world.graph.complete(task_id, by=world.anna)
    world.graph.tasks[task_id]["completedDateTime"] = None
    await cycle(world, notifier)
    assert channel.titles == ["Write docs"]

    for _ in range(2):
        world.graph.touch(task_id)
        await cycle(world, notifier)
    assert channel.titles == ["Write docs"]


@async_test
async def test_reopening_and_completing_again_is_a_new_completion(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)

    world.graph.complete(task_id, by=world.anna)
    await cycle(world, notifier)
    world.graph.reopen(task_id)
    await cycle(world, notifier)
    world.graph.tick(10)
    world.graph.complete(task_id, by=world.jan)
    await cycle(world, notifier)

    assert [event.completed_by for event in channel.events] == ["Anna Kowalska", "Jan Nowak"]


@async_test
async def test_restart_does_not_repeat_notifications(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    world.graph.add_task(world.plan_id, "Done long ago", percent=100)
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)
    world.graph.complete(task_id, by=world.anna)
    await cycle(world, notifier)
    assert channel.titles == ["Write docs"]
    store.close()

    # The process restarts: everything in memory is gone, only the database file remains.
    world = restarted(world)
    store2, notifier2, channel2 = start(world, tmp_path)
    for _ in range(3):
        await cycle(world, notifier2)

    assert channel2.events == []
    assert store2.counts_sync() == {"tasks": 2, "sent": 1}


@async_test
async def test_a_notification_that_was_still_queued_survives_a_restart(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)
    channel.error = DeliveryError("Discord is down")
    world.graph.complete(task_id, by=world.anna)
    await cycle(world, notifier)  # detected and queued, but not delivered
    assert channel.events == []
    store.close()

    world = restarted(world)
    store2, notifier2, channel2 = start(world, tmp_path)
    await cycle(world, notifier2)
    await cycle(world, notifier2)

    assert channel2.titles == ["Write docs"] and channel2.events[0].completed_by == "Anna Kowalska"
    assert store2.counts_sync() == {"tasks": 1, "sent": 1}


@async_test
async def test_completion_during_downtime_is_announced_after_the_restart(tmp_path):
    world = make_world()
    watched = world.graph.add_task(world.plan_id, "Completed while the bot was down")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)
    store.close()

    world.graph.tick(3 * 3600)
    world.graph.complete(watched, by=world.jan)
    created_meanwhile = world.graph.add_task(
        world.plan_id, "Created and completed while down", percent=100, completed_by=world.anna
    )
    world.graph.tick(3600)

    world = restarted(world)
    store2, notifier2, channel2 = start(world, tmp_path)
    await cycle(world, notifier2)
    await cycle(world, notifier2)

    assert sorted(channel2.titles) == ["Completed while the bot was down", "Created and completed while down"]
    assert created_meanwhile in world.graph.tasks


@async_test
async def test_new_task_that_is_already_complete(tmp_path):
    world = make_world()
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)

    # Created and finished between two polls: announce it.
    world.graph.tick(20)
    world.graph.add_task(world.plan_id, "Quick win", percent=100, completed_by=world.anna)
    # An old finished task copied into the plan: its completion happened long ago, don't.
    world.graph.add_task(
        world.plan_id,
        "Copied from last year",
        percent=100,
        completed_at=datetime(2025, 3, 1, 9, 0, tzinfo=timezone.utc),
    )
    await cycle(world, notifier)

    assert channel.titles == ["Quick win"]


@async_test
async def test_a_stale_read_from_graph_does_not_cause_a_duplicate(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)
    world.graph.complete(task_id, by=world.anna)
    await cycle(world, notifier)
    assert len(channel.events) == 1

    # A lagging replica shows the task as open once more, then complete again with the
    # same completedDateTime. That is the same completion, not a new one.
    task = world.graph.tasks[task_id]
    completed = (task["completedDateTime"], task["completedBy"])
    task.update(percentComplete=0, completedDateTime=None, completedBy=None)
    world.graph.touch(task_id)
    await cycle(world, notifier)
    task.update(percentComplete=100, completedDateTime=completed[0], completedBy=completed[1])
    world.graph.touch(task_id)
    await cycle(world, notifier)

    assert len(channel.events) == 1


@async_test
async def test_undeliverable_notifications_wait_and_are_sent_in_order(tmp_path, caplog):
    world = make_world()
    first = world.graph.add_task(world.plan_id, "First")
    second = world.graph.add_task(world.plan_id, "Second")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)

    channel.error = DeliveryError("the bot may not post in channel 42", permanent=True)
    world.graph.complete(first)
    await cycle(world, notifier)
    world.graph.complete(second)
    with caplog.at_level(logging.ERROR):
        await cycle(world, notifier)
        await cycle(world, notifier)

    assert channel.events == []
    assert store.counts_sync() == {"tasks": 2, "pending": 2}
    assert channel.attempts == 3  # one try per round; the second notification waits behind the first
    problems = [r for r in caplog.records if "can't be posted" in r.getMessage()]
    assert len(problems) == 1 and "may not post in channel 42" in problems[0].getMessage()  # not repeated every round

    channel.error = None
    await cycle(world, notifier)
    assert channel.titles == ["First", "Second"]
    assert store.counts_sync() == {"tasks": 2, "sent": 2}


@async_test
async def test_unexpected_delivery_errors_do_not_lose_the_notification(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)
    channel.error = RuntimeError("something odd")
    world.graph.complete(task_id)
    await notifier.run_cycle()  # must not raise
    assert store.counts_sync()["pending"] == 1
    channel.error = None
    await notifier.run_cycle()
    assert channel.titles == ["Write docs"]


@async_test
async def test_completion_through_the_bot_is_credited_to_the_discord_user(tmp_path):
    world = make_world()
    world.graph.add_task(world.plan_id, "Write docs", bucket_id=world.todo)
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)

    result = await world.planner.complete_task("Write docs")
    await notifier.record_bot_completion(result.task, plan_title=result.plan.title, discord_user_id=4242)
    await asyncio.sleep(0.05)  # the notification is sent right away, not at the next poll

    assert len(channel.events) == 1
    event = channel.events[0]
    assert (event.title, event.discord_user_id, event.bucket, event.plan_title) == (
        "Write docs",
        4242,
        "To do",
        "Sprint Board",
    )
    assert event.completed_by is None  # Planner only knows the bot's own identity here

    for _ in range(2):
        await cycle(world, notifier)
    assert len(channel.events) == 1


@async_test
async def test_bot_completion_noticed_by_the_poller_first_is_still_one_notification(tmp_path):
    world = make_world()
    world.graph.add_task(world.plan_id, "Write docs")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)

    result = await world.planner.complete_task("Write docs")
    channel.error = DeliveryError("Discord is down")
    await cycle(world, notifier)  # the poller sees the completion and queues it
    await notifier.record_bot_completion(result.task, plan_title=result.plan.title, discord_user_id=4242)
    await asyncio.sleep(0.05)
    channel.error = None
    await cycle(world, notifier)

    assert len(channel.events) == 1 and channel.events[0].discord_user_id == 4242


@async_test
async def test_before_the_first_poll_a_bot_completion_is_still_announced_once(tmp_path):
    world = make_world()
    world.graph.add_task(world.plan_id, "Write docs")
    store, notifier, channel = start(world, tmp_path)

    result = await world.planner.complete_task("Write docs")
    await notifier.record_bot_completion(result.task, plan_title=result.plan.title, discord_user_id=7)
    await asyncio.sleep(0.05)
    await cycle(world, notifier)  # this poll is the plan's baseline
    await cycle(world, notifier)

    assert len(channel.events) == 1


@async_test
async def test_deleted_tasks_are_forgotten_and_paging_is_followed(tmp_path):
    world = make_world()
    ids = [world.graph.add_task(world.plan_id, f"Task {n}") for n in range(7)]
    world.graph.page_size = 3
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)
    assert store.counts_sync() == {"tasks": 7}

    world.graph.delete_task(ids[0])
    world.graph.complete(ids[6], by=world.anna)
    await cycle(world, notifier)

    assert store.counts_sync() == {"tasks": 6, "sent": 1}
    assert channel.titles == ["Task 6"]


@async_test
async def test_every_plan_of_the_group_is_watched_and_one_failing_plan_does_not_block_the_rest(tmp_path, caplog):
    world = make_world()
    marketing = next(p for p, plan in world.graph.plans.items() if plan["title"] == "Marketing")
    sprint_task = world.graph.add_task(world.plan_id, "Sprint task")
    marketing_task = world.graph.add_task(marketing, "Marketing task")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)

    world.graph.complete(sprint_task)
    world.graph.complete(marketing_task, by=world.jan)
    world.graph.fail_next(403, path=rf"/plans/{world.plan_id}/tasks$")
    with caplog.at_level(logging.WARNING):
        await cycle(world, notifier)
    assert channel.titles == ["Marketing task"]
    assert any("Skipping plan “Sprint Board”" in r.getMessage() for r in caplog.records)

    await cycle(world, notifier)  # next round the plan is readable again: nothing was lost
    assert channel.titles == ["Marketing task", "Sprint task"]
    assert {event.plan_title for event in channel.events} == {"Marketing", "Sprint Board"}


@async_test
async def test_a_plan_added_later_gets_its_own_baseline(tmp_path):
    world = make_world()
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)

    new_plan = world.graph.add_plan("Hiring")
    world.graph.add_task(new_plan, "Finished before the bot knew the plan", percent=100)
    later = world.graph.add_task(new_plan, "Interview candidates")
    await world.planner.plans(max_age=0)
    await cycle(world, notifier)
    assert channel.events == []

    world.graph.complete(later)
    await cycle(world, notifier)
    assert channel.titles == ["Interview candidates"]


@async_test
async def test_throttling_stops_the_round_and_asks_for_a_pause(tmp_path, caplog):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    store, notifier, channel = start(world, tmp_path, interval=60)
    await notifier.run_cycle()

    world.graph.complete(task_id)
    world.graph.fail_next(429, headers={"Retry-After": "300"}, path=r"/tasks$")
    with caplog.at_level(logging.WARNING):
        pause = await notifier.run_cycle()
    assert pause == 300
    assert channel.events == []
    assert any("throttling" in r.getMessage() for r in caplog.records)

    world.time.now += 301
    assert await notifier.run_cycle() == 0
    assert channel.titles == ["Write docs"]


@async_test
async def test_a_round_never_raises_and_the_heartbeat_follows_successful_checks(tmp_path, caplog):
    world = make_world()
    store, notifier, channel = start(world, tmp_path)
    heartbeat = tmp_path / "heartbeat"

    world.graph.fail_next(500, times=100)
    with caplog.at_level(logging.ERROR):
        assert await notifier.run_cycle() == 0
    assert any("Could not check Planner" in r.getMessage() for r in caplog.records)
    assert not heartbeat.exists()  # nothing was checked, so no sign of life

    world.graph._faults.clear()
    caplog.clear()
    world.graph.fail_next(403, path=r"/tasks$", times=2)  # every plan refuses its task list
    with caplog.at_level(logging.ERROR):
        await notifier.run_cycle()
    assert any("Could not check Planner" in r.getMessage() for r in caplog.records)
    assert not heartbeat.exists()

    await notifier.run_cycle()
    assert heartbeat.exists()
    now = heartbeat.stat().st_mtime
    assert is_healthy({"DATA_DIR": str(tmp_path)}, now + 299)
    assert not is_healthy({"DATA_DIR": str(tmp_path)}, now + 301)
    assert is_healthy({"DATA_DIR": str(tmp_path), "POLL_INTERVAL_SECONDS": "600"}, now + 1700)
    assert not is_healthy({"DATA_DIR": str(tmp_path / "missing")}, now)


@async_test
async def test_old_undelivered_notifications_expire_and_history_is_pruned(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    other = world.graph.add_task(world.plan_id, "Other")
    store, notifier, channel = start(world, tmp_path)
    await cycle(world, notifier)
    world.graph.complete(other)
    await cycle(world, notifier)  # delivered
    channel.error = DeliveryError("no access", permanent=True)
    world.graph.complete(task_id)
    await cycle(world, notifier)  # stuck
    assert store.counts_sync() == {"tasks": 2, "pending": 1, "sent": 1}

    now = world.graph.now
    assert store.housekeeping_sync(now - timedelta(hours=24), now - timedelta(days=30)) == (0, 0)
    later = now + timedelta(hours=25)
    assert store.housekeeping_sync(later - timedelta(hours=24), later - timedelta(days=30)) == (1, 0)
    assert store.counts_sync() == {"tasks": 2, "expired": 1, "sent": 1}
    much_later = now + timedelta(days=31)
    assert store.housekeeping_sync(much_later - timedelta(hours=24), much_later - timedelta(days=30)) == (0, 2)

    channel.error = None
    await cycle(world, notifier)
    assert channel.titles == ["Other"]  # the expired one is not sent late


@async_test
async def test_the_loop_runs_until_stopped(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    store, notifier, channel = start(world, tmp_path, interval=0.01)

    notifier.start()
    await asyncio.sleep(0.2)  # baseline happens in here
    world.graph.complete(task_id, by=world.anna)
    await asyncio.sleep(1.3)  # the loop never spins faster than once a second
    await notifier.stop()
    polls = len(world.graph.calls("GET", r"/tasks$"))
    await asyncio.sleep(0.1)

    assert channel.titles == ["Write docs"]
    assert len(world.graph.calls("GET", r"/tasks$")) == polls  # really stopped


def test_task_state_from_graph_reads_the_fields_that_matter():
    state = task_state_from_graph(
        {
            "id": "t1",
            "planId": "p1",
            "title": "Write docs",
            "bucketId": "b1",
            "percentComplete": 100,
            "completedDateTime": "2026-10-05T16:02:11.1234567Z",
            "completedBy": {"user": {"displayName": None, "id": "u1"}},
            "@odata.etag": 'W/"abc"',
        }
    )
    assert state.is_complete and state.completed_by == "u1" and state.etag == 'W/"abc"'
    assert state.event_key == "2026-10-05T16:02:11.123456+00:00"
    assert state.completed_at == datetime(2026, 10, 5, 16, 2, 11, 123456, tzinfo=timezone.utc)

    sparse = task_state_from_graph({"id": "t2", "percentComplete": None}, plan_id="p9")
    assert (sparse.plan_id, sparse.title, sparse.percent_complete, sparse.completed_by) == (
        "p9",
        "Untitled task",
        0,
        None,
    )
    assert not sparse.is_complete


def test_a_database_from_a_newer_version_is_refused(tmp_path):
    import sqlite3

    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="newer version"):
        StateStore(path).open()


@async_test
async def test_graph_throttled_reaches_poll_once_callers(tmp_path):
    world = make_world()
    store, notifier, channel = start(world, tmp_path)
    world.graph.fail_next(429, headers={"Retry-After": "600"})
    with pytest.raises(GraphThrottled):
        await notifier.poll_once()
