"""PlannerService against the fake Graph: creating tasks from names, labels, completing, listing."""

from __future__ import annotations

import logging
from datetime import date

import pytest

from graph_client import GraphForbidden
from helpers import TENANT_ID, async_test, make_world
from planner_service import PlannerService, SetupError, UserError

OTHER_GROUP = "33333333-aaaa-4bbb-8ccc-444444444444"


def last_post(world):
    return world.graph.calls("POST", r"^/planner/tasks$")[-1].json


# --------------------------------------------------------------------------- /task add


@async_test
async def test_create_task_resolves_every_name_to_an_id():
    world = make_world()

    created = await world.planner.create_task(
        title="  Fix   login  redirect ",
        plan="sprint board",
        bucket="in progress",
        labels="bug, FRONTEND",
        assignees="Anna Kowalska, jan.nowak@contoso.com",
        due=date(2026, 10, 19),
        description="Users land on a 404 after signing in.",
    )

    body = last_post(world)
    assert body == {
        "planId": world.plan_id,
        "title": "Fix login redirect",
        "bucketId": world.doing,
        "appliedCategories": {"category1": True, "category3": True},
        "assignments": {
            world.anna: {"@odata.type": "#microsoft.graph.plannerAssignment", "orderHint": " !"},
            world.jan: {"@odata.type": "#microsoft.graph.plannerAssignment", "orderHint": " !"},
        },
        "dueDateTime": "2026-10-19T10:00:00Z",
    }
    assert created.title == "Fix login redirect"
    assert created.plan.title == "Sprint Board"
    assert created.bucket.name == "In progress"
    assert created.labels == ("Bug", "Frontend")
    assert [m.display_name for m in created.assignees] == ["Anna Kowalska", "Jan Nowak"]
    assert created.url == (
        f"https://planner.cloud.microsoft/webui/plan/{world.plan_id}/view/board/task/{created.id}?tid={TENANT_ID}"
    )
    assert created.warnings == ()
    assert world.graph.task_details[created.id]["description"] == "Users land on a 404 after signing in."
    assert world.graph.tasks[created.id]["bucketId"] == world.doing


@async_test
async def test_minimal_task_goes_to_the_default_plan_and_first_bucket():
    world = make_world()
    created = await world.planner.create_task(title="Just a title")

    assert last_post(world) == {"planId": world.plan_id, "title": "Just a title", "bucketId": world.todo}
    assert created.bucket.name == "To do"
    assert created.labels == () and created.assignees == () and created.due is None


@async_test
async def test_default_bucket_from_the_configuration():
    by_name = make_world(default_bucket="in progress")
    await by_name.planner.create_task(title="A")
    assert last_post(by_name)["bucketId"] == by_name.doing

    by_id = make_world()
    by_id.planner._default_bucket = by_id.doing
    await by_id.planner.create_task(title="B")
    assert last_post(by_id)["bucketId"] == by_id.doing

    # A plan that has no bucket of that name falls back to its own first bucket.
    elsewhere = make_world(default_bucket="In progress")
    created = await elsewhere.planner.create_task(title="C", plan="Marketing")
    assert created.bucket.name == "Ideas"


@async_test
async def test_plan_can_be_given_by_title_or_id_and_must_be_in_the_group():
    world = make_world()
    foreign = world.graph.add_plan("Finance", group_id=OTHER_GROUP)
    marketing = next(p for p, plan in world.graph.plans.items() if plan["title"] == "Marketing")

    assert (await world.planner.create_task(title="A", plan="MARKETING")).plan.id == marketing
    assert (await world.planner.create_task(title="B", plan=marketing)).plan.id == marketing

    posts_before = len(world.graph.calls("POST"))
    for outside in ("Finance", foreign, "No such plan"):
        with pytest.raises(UserError, match="no plan called"):
            await world.planner.create_task(title="C", plan=outside)
    assert len(world.graph.calls("POST")) == posts_before  # nothing was created outside the group


@async_test
async def test_unknown_bucket_is_a_clear_error_and_nothing_is_created():
    world = make_world()
    with pytest.raises(UserError) as caught:
        await world.planner.create_task(title="A", bucket="In progres")
    message = str(caught.value)
    assert "no bucket called “In progres” in plan “Sprint Board”" in message
    assert "Did you mean “In progress”?" in message
    assert "To do, In progress, Done" in message
    assert world.graph.calls("POST") == []


@async_test
async def test_bucket_of_another_plan_is_rejected():
    world = make_world()
    with pytest.raises(UserError, match="no bucket called"):
        await world.planner.create_task(title="A", plan="Marketing", bucket=world.doing)


# --------------------------------------------------------------------------- labels


@async_test
async def test_labels_are_matched_by_name_to_category_slots():
    world = make_world()
    await world.planner.create_task(title="A", labels="Frontend")
    assert last_post(world)["appliedCategories"] == {"category3": True}

    # A label whose name contains a comma, a duplicate, and odd spacing.
    created = await world.planner.create_task(title="B", labels="needs review,urgent;  bug , Bug")
    assert last_post(world)["appliedCategories"] == {"category5": True, "category1": True}
    assert created.labels == ("Needs review, urgent", "Bug")


@async_test
async def test_unknown_label_is_a_clear_error():
    world = make_world()
    with pytest.raises(UserError) as caught:
        await world.planner.create_task(title="A", labels="Bug, Fronted")
    message = str(caught.value)
    assert "Label “Fronted” doesn't exist in plan “Sprint Board”." in message
    assert "Did you mean “Frontend”?" in message
    assert "Available labels: Bug, Frontend, Needs review, urgent." in message
    assert "/label create" in message
    assert world.graph.calls("POST") == []


@async_test
async def test_labels_belong_to_the_selected_plan():
    world = make_world()
    await world.planner.create_task(title="A", plan="Marketing", labels="Campaign")
    assert last_post(world)["appliedCategories"] == {"category2": True}
    with pytest.raises(UserError, match="Label “Bug” doesn't exist in plan “Marketing”"):
        await world.planner.create_task(title="B", plan="Marketing", labels="Bug")


@async_test
async def test_plan_without_named_labels():
    world = make_world()
    empty = world.graph.add_plan("Empty")
    world.graph.add_bucket(empty, "Inbox")
    with pytest.raises(UserError, match="no named labels yet"):
        await world.planner.create_task(title="A", plan="Empty", labels="Bug")


@async_test
async def test_label_create_names_the_first_unused_slot():
    world = make_world()
    await world.planner.create_task(title="Warm the label cache", labels="Bug")

    created = await world.planner.create_label("  Blocked  ")

    assert (created.slot, created.name, created.tasks_already_using_slot) == ("category2", "Blocked", 0)
    patch = world.graph.calls("PATCH", r"/details$")[-1]
    assert patch.json == {"categoryDescriptions": {"category2": "Blocked"}}
    assert patch.headers["If-Match"].startswith('W/"etag-')
    assert world.graph.plan_details[world.plan_id]["categoryDescriptions"]["category2"] == "Blocked"

    # The new label can be used straight away (the cached label list was dropped).
    await world.planner.create_task(title="A", labels="blocked")
    assert last_post(world)["appliedCategories"] == {"category2": True}


@async_test
async def test_label_create_avoids_a_colour_that_tasks_already_carry():
    world = make_world()
    world.graph.add_task(world.plan_id, "Tagged by colour", labels=("category2",))

    created = await world.planner.create_label("Blocked")

    assert created.slot == "category4"  # category2 is unnamed but in use


@async_test
async def test_label_create_says_so_when_only_used_colours_are_left():
    world = make_world()
    details = world.graph.plan_details[world.plan_id]["categoryDescriptions"]
    for number in range(1, 26):
        details[f"category{number}"] = f"Label {number}"
    details["category9"] = None
    world.graph.add_task(world.plan_id, "One", labels=("category9",))
    world.graph.add_task(world.plan_id, "Two", labels=("category9",))

    created = await world.planner.create_label("Blocked")

    assert (created.slot, created.tasks_already_using_slot) == ("category9", 2)


@async_test
async def test_label_create_rejects_duplicates_and_full_plans():
    world = make_world()
    with pytest.raises(UserError, match="already has a label called “Bug”"):
        await world.planner.create_label("bug")

    details = world.graph.plan_details[world.plan_id]["categoryDescriptions"]
    for number in range(1, 26):
        details[f"category{number}"] = f"Label {number}"
    with pytest.raises(UserError, match="All 25 labels"):
        await world.planner.create_label("One more")
    assert world.graph.calls("PATCH") == []


@async_test
async def test_label_create_recovers_from_an_etag_conflict():
    world = make_world()

    def someone_else_names_category2():
        details = world.graph.plan_details[world.plan_id]
        details["categoryDescriptions"]["category2"] = "Design"
        details["@odata.etag"] = world.graph._etag()

    world.graph.before_next("PATCH", r"/details$", someone_else_names_category2)

    created = await world.planner.create_label("Blocked")

    # First attempt: 412. Second attempt re-read the details and took the next free slot.
    labels = world.graph.plan_details[world.plan_id]["categoryDescriptions"]
    assert created.slot == "category4"
    assert (labels["category2"], labels["category4"]) == ("Design", "Blocked")
    assert len(world.graph.calls("PATCH", r"/details$")) == 2


@async_test
async def test_label_create_gives_up_politely_if_conflicts_never_stop():
    world = make_world()
    world.graph.fail_next(412, method="PATCH", times=10)
    with pytest.raises(UserError, match="keep changing"):
        await world.planner.create_label("Blocked")
    assert len(world.graph.calls("PATCH")) == 4


# --------------------------------------------------------------------------- assignees


@async_test
async def test_assignees_by_display_name_email_or_comma_name():
    world = make_world()
    created = await world.planner.create_task(title="A", assignees="kowalski, piotr; ANNA.KOWALSKA@contoso.com")
    assert [m.display_name for m in created.assignees] == ["Kowalski, Piotr", "Anna Kowalska"]
    assert set(last_post(world)["assignments"]) == {m.id for m in created.assignees}

    # Naming the same person twice assigns them once.
    again = await world.planner.create_task(title="B", assignees="Jan Nowak, jan.nowak@contoso.com")
    assert list(last_post(world)["assignments"]) == [world.jan] and len(again.assignees) == 1


@async_test
async def test_member_list_falls_back_to_a_plain_request_if_graph_refuses_query_options():
    world = make_world()
    world.graph.fail_next(400, path=r"/members$", body={"error": {"code": "Request_UnsupportedQuery", "message": "x"}})

    members = await world.planner.members()

    assert [m.display_name for m in members] == ["Anna Kowalska", "Jan Nowak", "Kowalski, Piotr"]
    first, second = world.graph.calls("GET", r"/members$")
    assert first.sent_params == {"$select": "id,displayName,mail,userPrincipalName", "$top": "999"}
    assert second.sent_params == {}


@async_test
async def test_only_people_can_be_assigned():
    world = make_world()  # the group also contains an application ("Some app")
    assert [m.display_name for m in await world.planner.members()] == ["Anna Kowalska", "Jan Nowak", "Kowalski, Piotr"]
    assert await world.planner.suggest_assignees("some") == []
    with pytest.raises(UserError, match="Nobody in the plan's group is called “Some app”"):
        await world.planner.create_task(title="A", assignees="Some app")


@async_test
async def test_unknown_and_ambiguous_assignees():
    world = make_world()
    world.graph.add_member("Jan Nowak", "jan.nowak2@contoso.com")

    with pytest.raises(UserError) as unknown:
        await world.planner.create_task(title="A", assignees="Ana Kowalska")
    assert "Nobody in the plan's group is called “Ana Kowalska”. Did you mean “Anna Kowalska”?" in str(unknown.value)

    with pytest.raises(UserError) as ambiguous:
        await world.planner.create_task(title="A", assignees="Jan Nowak")
    assert "“Jan Nowak” matches 2 people" in str(ambiguous.value)
    assert "jan.nowak@contoso.com, jan.nowak2@contoso.com" in str(ambiguous.value)

    # The email settles it.
    created = await world.planner.create_task(title="A", assignees="jan.nowak2@contoso.com")
    assert created.assignees[0].email == "jan.nowak2@contoso.com"
    assert world.graph.calls("POST")[-1].json["assignments"]


@async_test
async def test_all_mistakes_are_reported_together():
    world = make_world()
    with pytest.raises(UserError) as caught:
        await world.planner.create_task(title="A", bucket="Nope", labels="Nope", assignees="Nope")
    message = str(caught.value)
    assert message.count("\n") == 2
    assert "no bucket called “Nope”" in message
    assert "Label “Nope” doesn't exist" in message
    assert "Nobody in the plan's group is called “Nope”" in message
    assert world.graph.calls("POST") == []


@async_test
async def test_members_without_names_are_reported_once(caplog):
    world = make_world()
    for member in world.graph.members:
        member.update(displayName=None, mail=None, userPrincipalName=None)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(UserError, match="Nobody in the plan's group is called “Anna Kowalska”"):
            await world.planner.create_task(title="A", assignees="Anna Kowalska")
        await world.planner.members(max_age=0)

    warnings = [r for r in caplog.records if "without names" in r.getMessage()]
    assert len(warnings) == 1 and "User.ReadBasic.All" in warnings[0].getMessage()


# --------------------------------------------------------------------------- titles, descriptions, errors


@async_test
async def test_title_is_validated_before_anything_is_sent():
    world = make_world()
    with pytest.raises(UserError, match="needs a title"):
        await world.planner.create_task(title="   ")
    with pytest.raises(UserError, match="too long"):
        await world.planner.create_task(title="x" * 256)
    assert world.graph.requests == []


@async_test
async def test_description_waits_for_the_details_of_a_new_task():
    world = make_world()
    world.graph.details_lag = 2  # the details answer 404 twice before they exist

    created = await world.planner.create_task(title="A", description="Notes")

    assert world.graph.task_details[created.id]["description"] == "Notes"
    assert created.warnings == ()
    patch = world.graph.calls("PATCH", r"/details$")[-1]
    assert patch.json == {"description": "Notes"} and "If-Match" in patch.headers
    assert world.time.sleeps == [0.4, 0.8]


@async_test
async def test_task_survives_a_description_that_cannot_be_saved():
    world = make_world()
    world.graph.fail_next(403, method="PATCH", path=r"/details$")

    created = await world.planner.create_task(title="A", description="Notes")

    assert created.id in world.graph.tasks
    assert len(created.warnings) == 1 and "description couldn't be saved" in created.warnings[0]


@async_test
async def test_graph_errors_while_creating_propagate_and_reset_the_caches():
    world = make_world()
    await world.planner.create_task(title="Warm the caches")
    world.graph.fail_next(403, method="POST", body={"error": {"code": "MaximumTasksInProject", "message": "Limit"}})
    with pytest.raises(GraphForbidden) as caught:
        await world.planner.create_task(title="A")
    assert caught.value.code == "MaximumTasksInProject"


# --------------------------------------------------------------------------- caching


@async_test
async def test_lookups_are_cached():
    world = make_world()
    for number in range(3):
        await world.planner.create_task(title=f"Task {number}", labels="Bug", assignees="Jan Nowak")

    assert len(world.graph.calls("GET", r"/planner/plans$")) == 1
    assert len(world.graph.calls("GET", r"/buckets$")) == 1
    assert len(world.graph.calls("GET", r"/plans/[^/]+/details$")) == 1
    assert len(world.graph.calls("GET", r"/members$")) == 1
    assert len(world.graph.calls("POST")) == 3


@async_test
async def test_a_bucket_or_label_newer_than_the_cache_is_still_found():
    world = make_world()
    await world.planner.create_task(title="Warm the caches", labels="Bug")
    review = world.graph.add_bucket(world.plan_id, "Review")
    world.graph.plan_details[world.plan_id]["categoryDescriptions"]["category7"] = "Docs"
    # Wind the cache entries back so they are older than the few-second re-check window.
    for cache in (world.planner._buckets, world.planner._labels):
        value, loaded = cache._values[world.plan_id]
        cache._values[world.plan_id] = (value, loaded - 30)

    created = await world.planner.create_task(title="A", bucket="Review", labels="Docs")

    assert created.bucket.id == review and created.labels == ("Docs",)


@async_test
async def test_a_typo_does_not_hammer_graph():
    world = make_world()
    for _ in range(5):
        with pytest.raises(UserError):
            await world.planner.create_task(title="A", bucket="Nope")
    # One load, not one per attempt: a list fetched seconds ago is not fetched again.
    assert len(world.graph.calls("GET", r"/buckets$")) == 1


# --------------------------------------------------------------------------- /task complete


@async_test
async def test_complete_by_title_or_id():
    world = make_world()
    first = world.graph.add_task(world.plan_id, "Write docs", bucket_id=world.doing)
    second = world.graph.add_task(world.plan_id, "Ship it")

    by_title = await world.planner.complete_task("write DOCS")
    assert by_title.changed and by_title.summary.title == "Write docs" and by_title.summary.bucket == "In progress"
    assert by_title.task["percentComplete"] == 100 and by_title.task["completedDateTime"]
    assert world.graph.tasks[first]["percentComplete"] == 100

    by_id = await world.planner.complete_task(second)
    assert by_id.changed and world.graph.tasks[second]["percentComplete"] == 100
    patch = world.graph.calls("PATCH")[-1]
    assert patch.json == {"percentComplete": 100}
    assert patch.headers["Prefer"] == "return=representation" and "If-Match" in patch.headers


@async_test
async def test_complete_retries_with_a_new_etag_after_a_conflict():
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    world.graph.before_next("PATCH", rf"/tasks/{task_id}$", lambda: world.graph.touch(task_id))

    result = await world.planner.complete_task("Write docs")

    assert result.changed and world.graph.tasks[task_id]["percentComplete"] == 100
    patches = world.graph.calls("PATCH")
    assert len(patches) == 2 and patches[0].headers["If-Match"] != patches[1].headers["If-Match"]


@async_test
async def test_complete_reports_endless_conflicts_politely():
    world = make_world()
    world.graph.add_task(world.plan_id, "Write docs")
    world.graph.fail_next(412, method="PATCH", times=10)
    with pytest.raises(UserError, match="keeps changing"):
        await world.planner.complete_task("Write docs")
    assert len(world.graph.calls("PATCH")) == 4


@async_test
async def test_completing_a_finished_task_changes_nothing():
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs", percent=100)
    result = await world.planner.complete_task(task_id)
    assert not result.changed and world.graph.calls("PATCH") == []


@async_test
async def test_complete_handles_a_204_answer():
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    world.graph.honour_prefer = False  # Graph applies the change but answers 204 No Content

    result = await world.planner.complete_task(task_id)

    assert result.changed and result.task["percentComplete"] == 100 and result.task["completedDateTime"]


@async_test
async def test_a_task_created_in_planner_moments_ago_can_be_listed_and_completed():
    world = make_world()
    world.graph.add_task(world.plan_id, "Old task")
    await world.planner.tasks(world.plan_id)  # e.g. the poller just ran
    cached, loaded = world.planner._tasks._values[world.plan_id]
    world.planner._tasks._values[world.plan_id] = (cached, loaded - 10)  # ... ten seconds ago
    fresh = world.graph.add_task(world.plan_id, "Brand new task")

    assert [task.title for task in (await world.planner.list_tasks()).tasks] == ["Brand new task", "Old task"]

    world.planner._tasks._values[world.plan_id] = (cached, world.planner._tasks._clock() - 10)  # stale again
    result = await world.planner.complete_task("brand new task")
    assert result.changed and world.graph.tasks[fresh]["percentComplete"] == 100


@async_test
async def test_the_bots_own_changes_do_not_hide_what_others_did_in_the_meantime():
    world = make_world()
    mine = world.graph.add_task(world.plan_id, "Mine")
    await world.planner.tasks(world.plan_id)
    cached, loaded = world.planner._tasks._values[world.plan_id]
    world.planner._tasks._values[world.plan_id] = (cached, loaded - 10)  # loaded ten seconds ago

    await world.planner.complete_task(mine)  # the bot changes one task ...
    world.graph.add_task(world.plan_id, "Added by a colleague")  # ... while someone adds another

    listing = await world.planner.list_tasks()
    assert [task.title for task in listing.tasks] == ["Added by a colleague"]


@async_test
async def test_complete_needs_an_unambiguous_task():
    world = make_world()
    world.graph.add_task(world.plan_id, "Write docs")
    world.graph.add_task(world.plan_id, "Write docs")
    world.graph.add_task(world.plan_id, "Write tests")

    with pytest.raises(UserError, match="2 tasks called “Write docs”"):
        await world.planner.complete_task("Write docs")
    with pytest.raises(UserError) as missing:
        await world.planner.complete_task("Write test")
    assert "couldn't find a task called “Write test”" in str(missing.value)
    assert "Did you mean “Write tests”?" in str(missing.value)
    with pytest.raises(UserError, match="which task"):
        await world.planner.complete_task("   ")


@async_test
async def test_complete_stays_inside_the_group_and_inside_the_url_path():
    world = make_world()
    foreign_plan = world.graph.add_plan("Finance", group_id=OTHER_GROUP)
    foreign_task = world.graph.add_task(foreign_plan, "Pay invoices")

    # The app's Graph permission would allow this; the bot must not. The answer is the same
    # as for a task that doesn't exist, so task IDs elsewhere in the tenant can't be probed.
    with pytest.raises(UserError) as outside:
        await world.planner.complete_task(foreign_task)
    with pytest.raises(UserError) as nonexistent:
        await world.planner.complete_task("task000000000000000000009999")
    assert "couldn't find a task" in str(outside.value)
    assert str(outside.value).replace(foreign_task, "X") == str(nonexistent.value).replace(
        "task000000000000000000009999", "X"
    )
    assert world.graph.tasks[foreign_task]["percentComplete"] == 0 and world.graph.calls("PATCH") == []

    # Text that isn't an ID never becomes part of a request path.
    before = len(world.graph.requests)
    with pytest.raises(UserError):
        await world.planner.complete_task("../../users")
    assert all("users" not in r.path for r in world.graph.requests[before:])


@async_test
async def test_complete_finds_a_task_of_another_plan_in_the_group_by_id():
    world = make_world()
    marketing = next(p for p, plan in world.graph.plans.items() if plan["title"] == "Marketing")
    task_id = world.graph.add_task(marketing, "Plan campaign")

    result = await world.planner.complete_task(task_id)  # no plan given: the default plan is searched first

    assert result.changed and result.plan.title == "Marketing"


# --------------------------------------------------------------------------- /task list


@async_test
async def test_list_open_tasks_sorted_by_due_date_with_names():
    world = make_world()
    world.graph.add_task(world.plan_id, "No due date", bucket_id=world.todo)
    world.graph.add_task(
        world.plan_id,
        "Later",
        bucket_id=world.doing,
        due="2026-11-01T10:00:00Z",
        assignees=(world.anna,),
        labels=("category1", "category9"),
    )
    world.graph.add_task(
        world.plan_id, "Sooner", bucket_id=world.todo, due="2026-10-10T10:00:00Z", assignees=(world.jan, "stranger")
    )
    world.graph.add_task(world.plan_id, "Finished", percent=100)

    listing = await world.planner.list_tasks()

    assert [task.title for task in listing.tasks] == ["Sooner", "Later", "No due date"]
    assert listing.total == 3 and not listing.completed and listing.plan.title == "Sprint Board"
    later = listing.tasks[1]
    assert (later.bucket, later.labels, later.assignees) == ("In progress", ("Bug",), ("Anna Kowalska",))
    assert listing.tasks[0].assignees == ("Jan Nowak", "Unknown")
    assert later.url.endswith(f"/task/{later.id}?tid={TENANT_ID}")


@async_test
async def test_odata_annotations_inside_a_task_are_not_mistaken_for_people_or_labels():
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Annotated", assignees=(world.anna,), labels=("category1",))
    task = world.graph.tasks[task_id]
    task["assignments"] = {"@odata.type": "#microsoft.graph.plannerAssignments", **task["assignments"]}
    task["appliedCategories"] = {
        "@odata.type": "#microsoft.graph.plannerAppliedCategories",
        **task["appliedCategories"],
    }

    listing = await world.planner.list_tasks()
    assert (listing.tasks[0].assignees, listing.tasks[0].labels) == (("Anna Kowalska",), ("Bug",))
    assert (await world.planner.list_tasks(assignee="Anna Kowalska")).total == 1
    assert (await world.planner.create_label("Blocked")).slot == "category2"


@async_test
async def test_list_filters_and_limit():
    world = make_world()
    for number in range(5):
        world.graph.add_task(world.plan_id, f"Todo {number}", bucket_id=world.todo, assignees=(world.anna,))
    world.graph.add_task(world.plan_id, "Doing", bucket_id=world.doing, assignees=(world.jan,))
    done = world.graph.add_task(world.plan_id, "Done already", percent=100)

    assert (await world.planner.list_tasks(bucket="in progress")).total == 1
    assert (await world.planner.list_tasks(assignee="jan.nowak@contoso.com")).tasks[0].title == "Doing"
    assert (await world.planner.list_tasks(assignee="Anna Kowalska, Jan Nowak")).total == 6
    limited = await world.planner.list_tasks(limit=3)
    assert len(limited.tasks) == 3 and limited.total == 6
    finished = await world.planner.list_tasks(completed=True)
    assert [task.id for task in finished.tasks] == [done] and finished.tasks[0].completed_at is not None
    with pytest.raises(UserError, match="no bucket called"):
        await world.planner.list_tasks(bucket="Nope")


# --------------------------------------------------------------------------- autocomplete


@async_test
async def test_suggestions():
    world = make_world()
    open_task = world.graph.add_task(world.plan_id, "Write docs", bucket_id=world.doing)
    world.graph.add_task(world.plan_id, "Write release notes", percent=100)
    marketing = next(p for p, plan in world.graph.plans.items() if plan["title"] == "Marketing")

    assert await world.planner.suggest_plans("") == [("Sprint Board", world.plan_id), ("Marketing", marketing)]
    assert await world.planner.suggest_plans("mark") == [("Marketing", marketing)]

    assert [name for name, _ in await world.planner.suggest_buckets(None, "")] == ["To do", "In progress", "Done"]
    assert await world.planner.suggest_buckets(None, "prog") == [("In progress", world.doing)]
    assert [name for name, _ in await world.planner.suggest_buckets("Marketing", "")] == ["Ideas"]
    assert [name for name, _ in await world.planner.suggest_buckets(marketing, "")] == ["Ideas"]
    assert await world.planner.suggest_buckets("No such plan", "") == []

    assert await world.planner.suggest_labels(None, "bug, fr") == ["Bug, Frontend"]
    assert await world.planner.suggest_labels("Marketing", "") == ["Campaign"]

    assert await world.planner.suggest_assignees("kow") == ["Kowalski, Piotr", "Anna Kowalska"]
    assert await world.planner.suggest_assignees("Anna Kowalska, jan.n") == ["Anna Kowalska, Jan Nowak"]

    # Only open tasks are offered for completion; the bucket is shown once it is known.
    await world.planner.buckets(world.plan_id)
    assert await world.planner.suggest_tasks(None, "write") == [("Write docs · In progress", open_task)]


@async_test
async def test_people_sharing_a_name_are_suggested_by_email():
    world = make_world()
    world.graph.add_member("Jan Nowak", "jan.nowak2@contoso.com")
    assert await world.planner.suggest_assignees("jan") == ["jan.nowak@contoso.com", "jan.nowak2@contoso.com"]


@async_test
async def test_suggestions_never_wait_long_for_graph():
    world = make_world()
    world.planner.FAST_TIMEOUT = 0.01

    async def slow_plans():
        import asyncio

        await asyncio.sleep(0.05)
        return []

    world.planner._load_plans = slow_plans
    assert await world.planner.suggest_plans("") == []
    assert await world.planner.suggest_buckets(None, "") == []


@async_test
async def test_suggestions_survive_graph_errors():
    world = make_world()
    world.graph.fail_next(503, times=50)
    assert await world.planner.suggest_plans("") == []
    assert await world.planner.suggest_assignees("") == []


# --------------------------------------------------------------------------- setup


@async_test
async def test_group_is_taken_from_the_default_plan():
    world = make_world()
    assert await world.planner.group_id() == world.graph.group_id
    assert len(world.graph.calls("GET", rf"^/planner/plans/{world.plan_id}$")) == 1
    await world.planner.group_id()
    assert len(world.graph.calls("GET", rf"^/planner/plans/{world.plan_id}$")) == 1


@async_test
async def test_setup_problems_are_explained():
    world = make_world()
    missing = PlannerService(world.client, default_plan_id="doesNotExist0000000000000000", tenant_id=TENANT_ID)
    with pytest.raises(SetupError, match="DEFAULT_PLAN_ID wasn't found"):
        await missing.plans()

    roster_plan = world.graph.add_plan("Personal", container_type="roster")
    roster = PlannerService(world.client, default_plan_id=roster_plan, tenant_id=TENANT_ID)
    with pytest.raises(SetupError, match="isn't owned by a Microsoft 365 group"):
        await roster.plans()

    foreign = world.graph.add_plan("Finance", group_id=OTHER_GROUP)
    mismatch = PlannerService(world.client, default_plan_id=foreign, tenant_id=TENANT_ID, group_id=world.graph.group_id)
    with pytest.raises(SetupError, match="isn't one of the plans of the configured group"):
        await mismatch.create_task(title="A")


@async_test
async def test_user_names_for_notifications():
    world = make_world()
    outsider = "00000000-0000-4000-8000-00000000abcd"
    world.graph.users[outsider] = {"id": outsider, "displayName": "Olga Outsider", "mail": "olga@contoso.com"}

    assert await world.planner.user_name(world.anna) == "Anna Kowalska"  # a group member: no extra request
    assert world.graph.calls("GET", r"^/users/") == []
    assert await world.planner.user_name(outsider) == "Olga Outsider"  # looked up individually
    assert await world.planner.user_name("unknown-id") is None  # e.g. the app's own identity
    assert await world.planner.user_name("unknown-id") is None
    assert len(world.graph.calls("GET", r"^/users/unknown-id$")) == 1  # the miss is remembered
    assert await world.planner.user_name(None) is None

    world.graph.fail_next(403, path=r"^/users/")  # app-only without User.Read.All
    assert await world.planner.user_name("another-id") is None


@async_test
async def test_bucket_name_lookup_notices_new_buckets():
    world = make_world()
    assert await world.planner.bucket_name(world.plan_id, world.doing) == "In progress"
    assert await world.planner.bucket_name(world.plan_id, None) is None
    assert await world.planner.bucket_name(world.plan_id, "gone") is None
