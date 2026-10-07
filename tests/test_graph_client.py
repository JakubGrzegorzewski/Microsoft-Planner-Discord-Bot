"""GraphClient: throttling, token refresh, transient errors, paging, error mapping."""

from __future__ import annotations

import pytest

from fake_graph import BASE_URL
from graph_client import (
    GraphAuthError,
    GraphBadRequest,
    GraphError,
    GraphForbidden,
    GraphNotFound,
    GraphPreconditionFailed,
    GraphThrottled,
    GraphUnavailable,
    parse_retry_after,
)
from helpers import async_test, make_world


@async_test
async def test_429_waits_for_retry_after_then_succeeds():
    world = make_world()
    world.graph.fail_next(429, headers={"Retry-After": "7"})

    plan = await world.client.get(f"/planner/plans/{world.plan_id}")

    assert plan["title"] == "Sprint Board"
    assert world.time.sleeps == [7.0]
    assert len(world.graph.calls("GET", r"^/planner/plans/[^/]+$")) == 2


@async_test
async def test_429_longer_than_the_caller_can_wait_raises_with_the_delay():
    world = make_world()
    world.graph.fail_next(429, headers={"Retry-After": "120"})

    with pytest.raises(GraphThrottled) as caught:
        await world.client.get(f"/planner/plans/{world.plan_id}", max_wait=20)

    assert caught.value.retry_after == 120
    assert world.time.sleeps == []  # it did not sit out part of a wait it could not finish


@async_test
async def test_other_requests_hold_back_while_a_throttle_is_in_force():
    world = make_world()
    world.graph.fail_next(429, headers={"Retry-After": "120"})
    with pytest.raises(GraphThrottled):
        await world.client.get(f"/planner/plans/{world.plan_id}", max_wait=20)
    sent_before = len(world.graph.requests)

    # A second request must not even be sent: it would only prolong the throttle.
    with pytest.raises(GraphThrottled) as caught:
        await world.client.get(f"/planner/plans/{world.plan_id}/buckets", max_wait=20)
    assert len(world.graph.requests) == sent_before
    assert 0 < caught.value.retry_after <= 120

    # Once the window has passed, requests flow again.
    world.time.now += 121
    assert await world.client.get(f"/planner/plans/{world.plan_id}/buckets")


@async_test
async def test_429_without_retry_after_backs_off_exponentially():
    world = make_world()
    world.graph.fail_next(429, times=3)

    await world.client.get(f"/planner/plans/{world.plan_id}")

    assert len(world.time.sleeps) == 3
    for delay, base in zip(world.time.sleeps, (1, 2, 4)):
        assert base <= delay <= base + 0.5


@async_test
async def test_retry_after_given_as_a_date():
    seconds = parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert seconds is not None and seconds > 1_000_000
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("-5") == 0.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


@async_test
async def test_401_gets_a_new_token_and_retries_once():
    world = make_world()
    await world.client.get(f"/planner/plans/{world.plan_id}")
    assert world.tokens.issued == 1
    world.graph.valid_tokens.clear()  # the token expired or was revoked

    plan = await world.client.get(f"/planner/plans/{world.plan_id}")

    assert plan["id"] == world.plan_id
    assert (world.tokens.issued, world.tokens.forced) == (2, 1)
    assert world.graph.requests[-1].headers["Authorization"] == "Bearer token-2"


@async_test
async def test_401_twice_is_an_auth_error():
    world = make_world()
    world.graph.fail_next(401, times=2)

    with pytest.raises(GraphAuthError):
        await world.client.get(f"/planner/plans/{world.plan_id}")

    assert world.tokens.forced == 1  # exactly one refresh, no loop


@async_test
async def test_503_is_retried_even_for_a_post():
    world = make_world()
    world.graph.fail_next(503, method="POST")

    task = await world.client.post("/planner/tasks", json={"planId": world.plan_id, "title": "Retry me"})

    assert task["title"] == "Retry me"
    assert len(world.graph.tasks) == 1


@async_test
async def test_a_post_is_not_repeated_when_its_outcome_is_unknown():
    for failure in ({"status": 504}, {"exception": OSError("connection reset")}):
        world = make_world()
        world.graph.fail_next(method="POST", **failure)

        with pytest.raises(GraphUnavailable):
            await world.client.post("/planner/tasks", json={"planId": world.plan_id, "title": "Once only"})

        # Repeating it could create the task twice.
        assert len(world.graph.calls("POST")) == 1


@async_test
async def test_reads_and_etag_guarded_writes_survive_network_errors():
    world = make_world()
    world.graph.fail_next(exception=OSError("connection reset"), times=2)
    assert await world.client.get(f"/planner/plans/{world.plan_id}")

    task_id = world.graph.add_task(world.plan_id, "Write docs")
    etag = world.graph.tasks[task_id]["@odata.etag"]
    world.graph.fail_next(504, method="PATCH")
    await world.client.patch(f"/planner/tasks/{task_id}", json={"percentComplete": 100}, etag=etag)
    assert world.graph.tasks[task_id]["percentComplete"] == 100


@async_test
async def test_gives_up_on_a_server_that_keeps_failing():
    world = make_world(max_attempts=3, default_wait=60)
    world.graph.fail_next(503, times=10)

    with pytest.raises(GraphUnavailable):
        await world.client.get(f"/planner/plans/{world.plan_id}")

    assert len(world.graph.requests) == 3


@async_test
async def test_stale_etag_raises_precondition_failed():
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    old_etag = world.graph.tasks[task_id]["@odata.etag"]
    world.graph.touch(task_id)

    with pytest.raises(GraphPreconditionFailed) as caught:
        await world.client.patch(f"/planner/tasks/{task_id}", json={"percentComplete": 100}, etag=old_etag)

    assert caught.value.status == 412
    assert world.graph.requests[-1].headers["If-Match"] == old_etag


@async_test
async def test_prefer_header_returns_the_updated_object():
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    etag = world.graph.tasks[task_id]["@odata.etag"]

    updated = await world.client.patch(
        f"/planner/tasks/{task_id}", json={"percentComplete": 100}, etag=etag, return_representation=True
    )
    assert updated["percentComplete"] == 100 and updated["@odata.etag"] != etag
    assert world.graph.requests[-1].headers["Prefer"] == "return=representation"

    silent = await world.client.patch(f"/planner/tasks/{task_id}", json={"title": "Docs"}, etag=updated["@odata.etag"])
    assert silent is None  # 204 No Content


@async_test
async def test_errors_carry_status_message_and_request_id():
    world = make_world()

    with pytest.raises(GraphNotFound) as missing:
        await world.client.get("/planner/tasks/doesnotexist")
    assert missing.value.status == 404
    assert missing.value.request_id == "req-test"
    assert "not found" in str(missing.value)

    world.graph.fail_next(403, body={"error": {"code": "MaximumTasksInProject", "message": "Too many tasks."}})
    with pytest.raises(GraphForbidden) as limit:
        await world.client.post("/planner/tasks", json={"planId": world.plan_id, "title": "x"})
    assert limit.value.code == "MaximumTasksInProject"

    with pytest.raises(GraphBadRequest):
        await world.client.post("/planner/tasks", json={"planId": "nope", "title": "x"})

    world.graph.fail_next(500, body="<html>Bad gateway</html>")
    with pytest.raises(GraphUnavailable) as html:
        await world.client.post("/planner/tasks", json={"planId": world.plan_id, "title": "x"})
    assert "Bad gateway" in html.value.message


@async_test
async def test_get_all_follows_next_links():
    world = make_world()
    for number in range(7):
        world.graph.add_task(world.plan_id, f"Task {number}")
    world.graph.page_size = 3

    tasks = await world.client.get_all(f"/planner/plans/{world.plan_id}/tasks")

    assert [task["title"] for task in tasks] == [f"Task {n}" for n in range(7)]
    pages = world.graph.calls("GET", r"/tasks$")
    assert [page.params.get("$skiptoken") for page in pages] == [None, "3", "6"]


@async_test
async def test_query_parameters_are_only_sent_with_the_first_page():
    world = make_world()
    for number in range(5):
        world.graph.add_task(world.plan_id, f"Task {number}")
    world.graph.page_size = 2

    await world.client.get_all(f"/planner/plans/{world.plan_id}/tasks", params={"$select": "id,title"})

    # A nextLink is complete as Graph wrote it; adding the parameters again could corrupt it.
    pages = world.graph.calls("GET", r"/tasks$")
    assert [page.sent_params for page in pages] == [{"$select": "id,title"}, {}, {}]


@async_test
async def test_a_next_link_to_another_host_is_refused():
    world = make_world()
    world.graph.fail_next(200, body={"value": [], "@odata.nextLink": "https://evil.example/v1.0/steal"})

    with pytest.raises(GraphError, match="outside Microsoft Graph"):
        await world.client.get_all(f"/planner/plans/{world.plan_id}/tasks")

    # The bearer token was only ever sent to the real base URL.
    assert len(world.graph.requests) == 1
    assert BASE_URL.startswith("https://graph.test")


@async_test
async def test_query_parameters_are_passed_through():
    world = make_world()
    await world.client.get_all(
        f"/groups/{world.graph.group_id}/members", params={"$select": "id,displayName", "$top": "999"}
    )
    assert world.graph.requests[-1].params == {"$select": "id,displayName", "$top": "999"}
