"""Completion notifications.

Microsoft Graph has no change notifications (webhooks) for Planner, so the bot polls: once
per interval it lists the tasks of every plan in the group and compares them with the state
saved in SQLite. A task whose percentComplete has *become* 100 since the last look produces
exactly one notification:

1. ``StateStore.apply_snapshot`` saves the new state and queues the notification in the
   same transaction, so a crash can neither lose a completion nor announce it twice.
2. ``flush`` sends what is queued and marks it as sent. If Discord can't be reached the
   notification simply stays queued for the next round.

The first snapshot of a plan is only recorded (the baseline): tasks that were already
complete before the bot started watching are never announced.

Nothing here imports discord. The bot passes in a ``deliver`` coroutine that turns a
``CompletionEvent`` into a message.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Optional

from graph_client import GraphError, GraphThrottled
from planner_service import PlannerService, UserError, parse_graph_datetime
from state import Notification, StateStore, TaskState, from_iso

log = logging.getLogger(__name__)

BATCH_SIZE = 25
MAX_BATCHES_PER_FLUSH = 4
HOUSEKEEPING_EVERY = 3600.0
REPEAT_DELIVERY_ERROR_EVERY = 900.0


@dataclass(frozen=True)
class CompletionEvent:
    """Everything a completion message needs, with names already looked up."""

    task_id: str
    title: str
    url: str
    plan_title: str
    bucket: Optional[str]
    completed_by: Optional[str]  # name from Microsoft 365, when it could be found
    completed_at: datetime
    discord_user_id: Optional[int]  # set when the task was completed with /task complete


class DeliveryError(Exception):
    """The message could not be posted. `permanent` means retrying won't help until someone fixes the setup."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def task_state_from_graph(task: dict[str, Any], plan_id: Optional[str] = None) -> TaskState:
    completed_by = ((task.get("completedBy") or {}).get("user") or {}).get("id")
    return TaskState(
        task_id=str(task["id"]),
        plan_id=str(task.get("planId") or plan_id or ""),
        title=str(task.get("title") or "Untitled task"),
        bucket_id=task.get("bucketId") or None,
        percent_complete=int(task.get("percentComplete") or 0),
        completed_at=parse_graph_datetime(task.get("completedDateTime")),
        completed_by=str(completed_by) if completed_by else None,
        etag=task.get("@odata.etag"),
    )


class CompletionNotifier:
    def __init__(
        self,
        *,
        planner: PlannerService,
        store: StateStore,
        deliver: Callable[[CompletionEvent], Awaitable[None]],
        interval: float = 60.0,
        heartbeat_path: Optional[Path] = None,
        give_up_after: timedelta = timedelta(hours=24),
        keep_history: timedelta = timedelta(days=30),
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._planner = planner
        self._store = store
        self._deliver = deliver
        self._interval = interval
        self._heartbeat_path = heartbeat_path
        self._give_up_after = give_up_after
        self._keep_history = keep_history
        self._now = now
        self._stop = asyncio.Event()
        self._flush_lock = asyncio.Lock()
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._background: set[asyncio.Task[Any]] = set()
        self._last_housekeeping = float("-inf")
        self._last_delivery_error: Optional[str] = None
        self._last_delivery_error_at = float("-inf")

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._loop_task is None:
            self._stop.clear()
            self._loop_task = asyncio.create_task(self.run_forever(), name="planner-completion-watcher")

    async def stop(self) -> None:
        self._stop.set()
        tasks = [task for task in (self._loop_task, *self._background) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = None

    async def run_forever(self) -> None:
        log.info("Watching Planner for completed tasks every %.0f seconds", self._interval)
        while not self._stop.is_set():
            started = time.monotonic()
            pause = await self.run_cycle()
            delay = max(self._interval - (time.monotonic() - started), pause, 1.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def run_cycle(self) -> float:
        """One round of poll, send, upkeep. Never raises. Returns how long Graph asked us to back off."""
        pause = 0.0
        try:
            await self.poll_once()
            self._touch_heartbeat()
        except GraphThrottled as exc:
            pause = float(exc.retry_after or self._interval)
            log.warning(
                "Microsoft Graph is throttling the bot; checking again in %.0f seconds", max(pause, self._interval)
            )
        except (GraphError, UserError) as exc:
            log.error("Could not check Planner for completed tasks: %s", exc)
        except Exception:
            log.exception("Unexpected error while checking Planner for completed tasks")

        try:
            await self.flush()
        except Exception:
            log.exception("Unexpected error while sending completion notifications")

        try:
            await self._planner.keep_warm()
        except (GraphError, UserError) as exc:
            log.debug("Could not refresh the Planner caches: %s", exc)  # the poll above reported it already
        except Exception:
            log.exception("Unexpected error while refreshing the Planner caches")

        await self._housekeeping()
        return pause

    # ------------------------------------------------------------------ detecting

    async def poll_once(self) -> int:
        """Look at every plan once. Returns the number of completions newly queued.

        A plan that can't be read is skipped for this round; if none can be read, the
        error is raised so the round counts as failed.
        """
        queued = 0
        readable = 0
        failure: Optional[GraphError] = None
        plans = await self._planner.plans()
        for plan in plans:
            started_at = self._now()
            try:
                tasks = await self._planner.tasks(plan.id, max_age=0)
            except GraphThrottled:
                raise  # no point asking about the other plans right now
            except GraphError as exc:
                failure = exc
                log.warning("Skipping plan “%s” this round: %s", plan.title, exc)
                continue
            readable += 1
            states = [task_state_from_graph(task, plan.id) for task in tasks if task.get("id")]
            result = await self._store.apply_snapshot(plan.id, plan.title, states, started_at=started_at)
            if result.baseline:
                done = sum(1 for state in states if state.is_complete)
                log.info(
                    "Now watching plan “%s”: %d tasks, %d already complete (those are not announced)",
                    plan.title,
                    result.seen,
                    done,
                )
            elif result.completions:
                log.info("Plan “%s”: %d task(s) newly completed", plan.title, result.completions)
            queued += result.completions
        if failure is not None and not readable:
            raise failure
        return queued

    async def record_bot_completion(self, task: dict[str, Any], *, plan_title: str, discord_user_id: int) -> None:
        """Called after /task complete: queue the notification now, credited to the Discord user."""
        state = task_state_from_graph(task)
        waiting = await self._store.record_completion(
            state, plan_title, discord_user_id=discord_user_id, now=self._now()
        )
        if waiting:
            self._spawn(self.flush())

    # ------------------------------------------------------------------ sending

    async def flush(self) -> int:
        """Send queued notifications in order. Returns how many were sent."""
        sent = 0
        async with self._flush_lock:
            for _ in range(MAX_BATCHES_PER_FLUSH):
                batch = await self._store.pending(BATCH_SIZE)
                if not batch:
                    break
                for notification in batch:
                    event = await self._event_for(notification)
                    try:
                        await self._deliver(event)
                    except DeliveryError as exc:
                        await self._store.mark_failed(notification.id, str(exc))
                        self._report_delivery_problem(str(exc))
                        return sent  # the rest would fail the same way; they stay queued
                    except Exception as exc:
                        await self._store.mark_failed(notification.id, f"{exc.__class__.__name__}: {exc}")
                        log.exception("Unexpected error while announcing task %s", notification.task_id)
                        return sent
                    await self._store.mark_sent(notification.id, self._now())
                    sent += 1
        if sent:
            self._last_delivery_error = None
            log.info("Posted %d completion notification(s)", sent)
        return sent

    async def _event_for(self, notification: Notification) -> CompletionEvent:
        payload = notification.payload
        bucket: Optional[str] = None
        completed_by: Optional[str] = None
        try:
            bucket = await self._planner.bucket_name(notification.plan_id, payload.get("bucket_id"))
            if notification.discord_user_id is None:
                completed_by = await self._planner.user_name(payload.get("completed_by"))
        except Exception as exc:
            # Names are nice to have. A lookup problem must not hold the notification back.
            log.warning("Could not look up names for the notification about task %s: %s", notification.task_id, exc)
        try:
            completed_at = from_iso(payload["completed_at"])
        except (KeyError, TypeError, ValueError):
            completed_at = notification.created_at
        return CompletionEvent(
            task_id=notification.task_id,
            title=str(payload.get("title") or "Untitled task"),
            url=self._planner.task_url(notification.plan_id, notification.task_id),
            plan_title=str(payload.get("plan_title") or ""),
            bucket=bucket,
            completed_by=completed_by,
            completed_at=completed_at,
            discord_user_id=notification.discord_user_id,
        )

    def _report_delivery_problem(self, message: str) -> None:
        now = time.monotonic()
        is_new = message != self._last_delivery_error
        if is_new or now - self._last_delivery_error_at >= REPEAT_DELIVERY_ERROR_EVERY:
            log.error("Completion notifications can't be posted: %s. They stay queued and are retried.", message)
            self._last_delivery_error = message
            self._last_delivery_error_at = now

    # ------------------------------------------------------------------ upkeep

    async def _housekeeping(self) -> None:
        if time.monotonic() - self._last_housekeeping < HOUSEKEEPING_EVERY:
            return
        self._last_housekeeping = time.monotonic()
        try:
            now = self._now()
            expired, _ = await self._store.housekeeping(
                expire_before=now - self._give_up_after, prune_before=now - self._keep_history
            )
        except Exception:
            log.exception("Unexpected error during database upkeep")
            return
        if expired:
            log.warning(
                "Gave up on %d notification(s) that could not be posted for %s",
                expired,
                self._give_up_after,
            )

    def _touch_heartbeat(self) -> None:
        """Record that Planner was just checked successfully (read by healthcheck.py)."""
        if self._heartbeat_path is None:
            return
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat_path.touch()
        except OSError as exc:
            log.debug("Could not update the heartbeat file: %s", exc)

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coroutine)
        self._background.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self._background.discard(done)
            if not done.cancelled() and done.exception() is not None:
                log.error("Background task failed", exc_info=done.exception())

        task.add_done_callback(finished)
