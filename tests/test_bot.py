"""The Discord layer: command definitions, role restriction, replies, autocomplete, error messages.

Commands are driven the way discord.py drives them (checks first, then the callback, errors
to the tree's handler) with a small fake interaction, against the fake Graph.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")
from discord import app_commands  # noqa: E402

import bot as bot_module  # noqa: E402
from config import load_config  # noqa: E402
from graph_client import (  # noqa: E402
    GraphAuthError,
    GraphBadRequest,
    GraphError,
    GraphForbidden,
    GraphNotFound,
    GraphPreconditionFailed,
    GraphThrottled,
    GraphUnavailable,
)
from helpers import async_test, make_world  # noqa: E402
from notifier import CompletionEvent, CompletionNotifier, DeliveryError  # noqa: E402
from planner_service import UserError  # noqa: E402
from test_config import VALID  # noqa: E402

GUILD = 500
ALLOWED_ROLE = 111
CHANNEL = int(VALID["NOTIFY_CHANNEL_ID"])


def plain(text: str) -> str:
    """Text with Markdown escapes removed, for comparing against what was typed."""
    return text.replace("\\", "")


class Response:
    def __init__(self) -> None:
        self.deferred: dict | None = None
        self.messages: list[dict] = []

    def is_done(self) -> bool:
        return self.deferred is not None or bool(self.messages)

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        assert not self.is_done(), "an interaction can only be answered once"
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}

    async def send_message(self, content=None, *, embed=None, ephemeral: bool = False) -> None:
        assert not self.is_done(), "an interaction can only be answered once"
        self.messages.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class Followup:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, content=None, *, embed=None, ephemeral: bool = False) -> None:
        self.messages.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class Interaction:
    """What a command sees of discord.Interaction."""

    def __init__(self, bot, *, roles=(ALLOWED_ROLE,), guild_id=GUILD, command="task add", **options) -> None:
        self.client = bot
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=1001, display_name="Jakub", roles=[SimpleNamespace(id=role) for role in roles])
        self.namespace = SimpleNamespace(**options)
        self.extras: dict = {}
        self.command = SimpleNamespace(qualified_name=command)
        self.response = Response()
        self.followup = Followup()
        self.original: dict | None = None
        self.original_deleted = False
        self.events: list[str] = []

    async def edit_original_response(self, *, content=None, embed=None) -> None:
        assert self.response.deferred is not None
        self.original = {"content": content, "embed": embed}
        self.events.append("edit")

    async def delete_original_response(self) -> None:
        self.original_deleted = True
        self.events.append("delete")

    @property
    def private_texts(self) -> list[str]:
        sent = [m for m in (*self.response.messages, *self.followup.messages) if m["ephemeral"]]
        return [m["content"] for m in sent if m["content"]]

    @property
    def anything_public(self) -> bool:
        public_message = any(not m["ephemeral"] for m in (*self.response.messages, *self.followup.messages))
        public_original = (
            self.original is not None and not self.response.deferred["ephemeral"] and not self.original_deleted
        )
        return public_message or public_original


class Channel:
    def __init__(self) -> None:
        self.guild = SimpleNamespace(id=GUILD)
        self.sent: list = []
        self.error: Exception | None = None

    async def send(self, content=None, *, embed=None) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(embed)


def make_bot(world, tmp_path, **settings):
    """A PlannerBot whose Graph side is the fake world and whose state lives in tmp_path."""
    config = load_config({**VALID, "DATA_DIR": str(tmp_path), **settings})
    bot = bot_module.PlannerBot(config)
    bot.graph = world.client
    bot.planner = world.planner
    bot.channel = Channel()
    bot.notifier = CompletionNotifier(
        planner=world.planner, store=bot.store, deliver=bot.deliver_completion, now=lambda: world.graph.now
    )

    async def ready() -> None:
        return None

    async def fetch_channel(channel_id):
        assert channel_id == CHANNEL
        return bot.channel

    bot.wait_until_ready = ready
    bot.get_channel = lambda channel_id: None
    bot.fetch_channel = fetch_channel
    return bot


async def invoke(command, interaction, **options) -> None:
    """Run a command like the command tree does: checks, callback, and errors to the tree's handler."""
    try:
        for check in command.checks:
            if not await check(interaction):
                raise app_commands.CheckFailure()
        await command.callback(interaction, **options)
    except Exception as error:
        if not isinstance(error, app_commands.AppCommandError):
            error = app_commands.CommandInvokeError(command, error)
        await interaction.client.tree.on_error(interaction, error)


def fields(embed) -> dict[str, str]:
    return {field.name: field.value for field in embed.fields}


# --------------------------------------------------------------------------- definitions


def test_commands_and_their_options():
    def options(command):
        return [(p.name, p.required, p.autocomplete) for p in command.parameters]

    assert [g.name for g in bot_module.COMMAND_GROUPS] == ["task", "label"]
    assert sorted(c.name for c in bot_module.task_group.commands) == ["add", "complete", "list"]
    assert [c.name for c in bot_module.label_group.commands] == ["create"]

    assert options(bot_module.task_group.get_command("add")) == [
        ("title", True, False),
        ("plan", False, True),
        ("bucket", False, True),
        ("labels", False, True),
        ("assignee", False, True),
        ("due_date", False, False),
        ("description", False, False),
    ]
    assert options(bot_module.task_group.get_command("list")) == [
        ("plan", False, True),
        ("bucket", False, True),
        ("assignee", False, True),
        ("completed", False, False),
    ]
    assert options(bot_module.task_group.get_command("complete")) == [("task", True, True), ("plan", False, True)]
    assert options(bot_module.label_group.get_command("create")) == [("name", True, False), ("plan", False, True)]


def test_every_command_is_restricted_to_the_allowed_roles():
    for group in bot_module.COMMAND_GROUPS:
        assert group.guild_only
        for command in group.commands:
            assert bot_module.require_allowed_role in command.checks, command.name


# --------------------------------------------------------------------------- permissions


@async_test
async def test_role_restriction(tmp_path):
    bot = make_bot(make_world(), tmp_path)

    def allowed(**details) -> bool:
        return bot_module.is_allowed(Interaction(bot, **details), bot.config.allowed_role_ids)

    assert allowed(roles=(ALLOWED_ROLE,))
    assert allowed(roles=(7, 222))
    assert not allowed(roles=(7, 8))
    assert not allowed(roles=())
    assert not allowed(roles=(ALLOWED_ROLE,), guild_id=None)  # a direct message

    everyone = make_bot(make_world(), tmp_path / "b", ALLOWED_ROLE_IDS=str(GUILD))
    assert bot_module.is_allowed(Interaction(everyone, roles=()), everyone.config.allowed_role_ids)  # @everyone

    nobody = make_bot(make_world(), tmp_path / "c", ALLOWED_ROLE_IDS="")
    assert not bot_module.is_allowed(Interaction(nobody), nobody.config.allowed_role_ids)


@async_test
async def test_members_without_an_allowed_role_are_refused_privately(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    interaction = Interaction(bot, roles=(7,))

    await invoke(bot_module.task_add, interaction, title="Sneaky")

    assert interaction.private_texts == ["⚠️ You don't have a role that is allowed to use this command."]
    assert not interaction.anything_public
    assert world.graph.requests == []


@async_test
async def test_autocomplete_tells_outsiders_nothing(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    outsider = Interaction(bot, roles=(7,))
    for callback in (
        bot_module.plan_autocomplete,
        bot_module.bucket_autocomplete,
        bot_module.labels_autocomplete,
        bot_module.assignee_autocomplete,
        bot_module.task_autocomplete,
    ):
        assert await callback(outsider, "") == []
    assert world.graph.requests == []


# --------------------------------------------------------------------------- /task add


@async_test
async def test_task_add_replies_with_a_public_embed(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    interaction = Interaction(bot)

    await invoke(
        bot_module.task_add,
        interaction,
        title="Fix login redirect",
        bucket="In progress",
        labels="Bug, Frontend",
        assignee="Anna Kowalska, jan.nowak@contoso.com",
        due_date="2026-10-19",
        description="Users land on a 404.",
    )

    assert interaction.response.deferred == {"ephemeral": False, "thinking": True}
    assert interaction.private_texts == [] and interaction.followup.messages == []
    embed = interaction.original["embed"]
    task_id = next(iter(world.graph.tasks))
    url = world.planner.task_url(world.plan_id, task_id)
    assert plain(embed.title) == "Fix login redirect"
    assert embed.url == url
    assert plain(embed.description) == "Users land on a 404."
    shown = {name: plain(value) for name, value in fields(embed).items()}
    due = int(datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc).timestamp())
    assert shown == {
        "Bucket": "In progress",
        "Labels": "Bug, Frontend",
        "Assignees": "Anna Kowalska, Jan Nowak",
        "Due date": f"<t:{due}:D>",
        "Plan": "Sprint Board",
        "Link": f"[Open in Planner]({url})",
    }
    assert embed.footer.text == "Task created by Jakub"
    assert world.graph.tasks[task_id]["dueDateTime"] == "2026-10-19T10:00:00Z"


@async_test
async def test_task_add_with_only_a_title(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    interaction = Interaction(bot)

    await invoke(bot_module.task_add, interaction, title="Just a title")

    embed = interaction.original["embed"]
    assert fields(embed)["Bucket"] == "To do"
    assert (fields(embed)["Labels"], fields(embed)["Assignees"], fields(embed)["Due date"]) == (
        "None",
        "Unassigned",
        "None",
    )
    assert embed.description is None


@async_test
async def test_a_bad_due_date_is_answered_privately_before_anything_happens(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    interaction = Interaction(bot)

    await invoke(bot_module.task_add, interaction, title="A", due_date="19.10.2026")

    assert interaction.response.deferred is None
    assert len(interaction.private_texts) == 1 and "isn't a valid due date" in interaction.private_texts[0]
    assert world.graph.requests == []


@async_test
async def test_a_wrong_name_is_explained_privately_and_leaves_nothing_behind(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    interaction = Interaction(bot)

    await invoke(bot_module.task_add, interaction, title="A", labels="Fronted")

    assert len(interaction.private_texts) == 1
    assert "Label “Fronted” doesn't exist in plan “Sprint Board”" in interaction.private_texts[0]
    assert "Traceback" not in interaction.private_texts[0]
    # The public "thinking…" message was resolved, the details sent privately, the leftover removed.
    assert interaction.events == ["edit", "delete"] and not interaction.anything_public
    assert world.graph.tasks == {}


@async_test
async def test_a_description_that_could_not_be_saved_is_mentioned_privately(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    world.graph.fail_next(403, method="PATCH", path=r"/details$")
    interaction = Interaction(bot)

    await invoke(bot_module.task_add, interaction, title="A", description="Notes")

    assert interaction.original["embed"] is not None
    assert len(interaction.private_texts) == 1 and "description couldn't be saved" in interaction.private_texts[0]


@async_test
async def test_unexpected_errors_are_logged_with_a_reference_not_shown(tmp_path, caplog):
    world = make_world()
    bot = make_bot(world, tmp_path)

    async def broken(**_):
        raise ZeroDivisionError("secret internals")

    bot.planner.create_task = broken
    interaction = Interaction(bot)
    with caplog.at_level(logging.ERROR):
        await invoke(bot_module.task_add, interaction, title="A")

    text = interaction.private_texts[0]
    assert "Something went wrong on my side" in text and "secret internals" not in text
    record = next(r for r in caplog.records if "Unexpected error in /task add" in r.getMessage())
    reference = record.getMessage().split("reference ")[1].rstrip(")")
    assert reference in text and record.exc_info is not None


# --------------------------------------------------------------------------- /task list, /task complete, /label create


@async_test
async def test_task_list_is_private_and_readable(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    world.graph.add_task(
        world.plan_id,
        "Write [docs]",
        bucket_id=world.doing,
        due="2026-10-10T10:00:00Z",
        assignees=(world.anna,),
        labels=("category1",),
    )
    world.graph.add_task(world.plan_id, "Finished", percent=100)
    interaction = Interaction(bot, command="task list")

    await invoke(bot_module.task_list, interaction)

    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    message = interaction.followup.messages[0]
    assert message["ephemeral"]
    embed = message["embed"]
    assert plain(embed.title) == "Open tasks · Sprint Board"
    line = plain(embed.description)
    assert line.startswith("• [Write (docs)](https://planner.cloud.microsoft/webui/plan/")
    assert "In progress" in line and "Anna Kowalska" in line and "Bug" in line and "due <t:" in line
    assert embed.footer.text == "1 task(s)"

    empty = Interaction(bot, command="task list")
    await invoke(bot_module.task_list, empty, bucket="Done")
    assert empty.followup.messages[0]["embed"].description == "Nothing to show."
    assert "bucket Done" in empty.followup.messages[0]["embed"].footer.text


@async_test
async def test_task_list_never_exceeds_discord_limits(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    for number in range(40):
        world.graph.add_task(
            world.plan_id, f"Task {number:02d} " + "x" * 200, bucket_id=world.todo, assignees=(world.anna, world.jan)
        )
    interaction = Interaction(bot, command="task list")

    await invoke(bot_module.task_list, interaction)

    embed = interaction.followup.messages[0]["embed"]
    assert len(embed.description) <= 4096 and len(embed) <= 6000
    assert embed.footer.text.startswith("Showing ") and embed.footer.text.endswith(" of 40")


@async_test
async def test_task_complete_marks_the_task_and_posts_the_notification(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    bot.store.open()
    task_id = world.graph.add_task(world.plan_id, "Write docs", bucket_id=world.doing)
    interaction = Interaction(bot, command="task complete")

    await invoke(bot_module.task_complete, interaction, task=task_id)
    await asyncio.sleep(0.05)

    assert world.graph.tasks[task_id]["percentComplete"] == 100
    assert interaction.response.deferred["ephemeral"]
    reply = interaction.private_texts[0]
    assert "Marked [Write docs](" in plain(reply) and f"<#{CHANNEL}>" in reply

    assert len(bot.channel.sent) == 1
    embed = bot.channel.sent[0]
    shown = {name: plain(value) for name, value in fields(embed).items()}
    assert plain(embed.title) == "✅ Write docs"
    assert embed.url == world.planner.task_url(world.plan_id, task_id)
    assert shown == {"Completed by": "<@1001> (via Discord)", "Bucket": "In progress", "Plan": "Sprint Board"}

    again = Interaction(bot, command="task complete")
    await invoke(bot_module.task_complete, again, task="Write docs")
    await asyncio.sleep(0.05)
    assert "was already completed" in again.private_texts[0]
    assert len(bot.channel.sent) == 1
    bot.store.close()


@async_test
async def test_label_create_reply(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    interaction = Interaction(bot, command="label create")

    await invoke(bot_module.label_create, interaction, name="Blocked")

    assert interaction.response.deferred["ephemeral"]
    assert plain(interaction.private_texts[0]).startswith("🏷️ Label **Blocked** now exists in plan **Sprint Board**.")
    assert world.graph.plan_details[world.plan_id]["categoryDescriptions"]["category2"] == "Blocked"

    duplicate = Interaction(bot, command="label create")
    await invoke(bot_module.label_create, duplicate, name="blocked")
    assert "already has a label called “Blocked”" in duplicate.private_texts[0]


# --------------------------------------------------------------------------- autocomplete


@async_test
async def test_autocomplete_choices(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    task_id = world.graph.add_task(world.plan_id, "Write docs")

    def names(choices):
        return [choice.name for choice in choices]

    plans = await bot_module.plan_autocomplete(Interaction(bot), "")
    assert names(plans) == ["Sprint Board", "Marketing"] and plans[0].value == world.plan_id

    assert names(await bot_module.bucket_autocomplete(Interaction(bot), "")) == ["To do", "In progress", "Done"]
    # The bucket, label and task suggestions follow the plan option as it is filled in so far.
    marketing = Interaction(bot, plan=plans[1].value)
    assert names(await bot_module.bucket_autocomplete(marketing, "")) == ["Ideas"]
    assert names(await bot_module.labels_autocomplete(marketing, "")) == ["Campaign"]

    labels = await bot_module.labels_autocomplete(Interaction(bot), "bug, fr")
    assert [(c.name, c.value) for c in labels] == [("Bug, Frontend", "Bug, Frontend")]
    assert names(await bot_module.assignee_autocomplete(Interaction(bot), "jan")) == ["Jan Nowak"]
    tasks = await bot_module.task_autocomplete(Interaction(bot), "wri")
    assert [(c.name, c.value) for c in tasks] == [("Write docs", task_id)]


@async_test
async def test_autocomplete_stays_within_discord_limits_and_never_raises(tmp_path, caplog):
    world = make_world()
    bot = make_bot(world, tmp_path)
    for number in range(40):
        world.graph.add_bucket(world.plan_id, f"Bucket {number:02d} " + "y" * 120)

    choices = await bot_module.bucket_autocomplete(Interaction(bot), "bucket")
    assert len(choices) == 25 and all(len(choice.name) <= 100 and len(choice.value) <= 100 for choice in choices)

    async def broken(current):
        raise RuntimeError("boom")

    bot.planner.suggest_plans = broken
    with caplog.at_level(logging.WARNING):
        assert await bot_module.plan_autocomplete(Interaction(bot), "") == []
    assert any("Autocomplete plan_autocomplete failed" in r.getMessage() for r in caplog.records)


@async_test
async def test_autocomplete_answers_before_discords_deadline_even_if_graph_is_slow(tmp_path, monkeypatch):
    world = make_world()
    bot = make_bot(world, tmp_path)
    monkeypatch.setattr(bot_module, "AUTOCOMPLETE_DEADLINE", 0.05)
    finished = []

    async def slow(current):
        await asyncio.sleep(0.5)
        finished.append(current)
        return [("Sprint Board", world.plan_id)]

    bot.planner.suggest_plans = slow
    started = asyncio.get_running_loop().time()
    assert await bot_module.plan_autocomplete(Interaction(bot), "spr") == []
    assert asyncio.get_running_loop().time() - started < 0.3


# --------------------------------------------------------------------------- error messages


def test_every_kind_of_failure_has_a_plain_message():
    cases = [
        (UserError("Label “x” doesn't exist."), "Label “x” doesn't exist."),
        (bot_module.NotAllowed(), "allowed to use this command"),
        (app_commands.NoPrivateMessage(), "only works inside the server"),
        (GraphThrottled("throttled", status=429, retry_after=42.7), "about 42 seconds"),
        (GraphAuthError("The client secret has expired."), "can't sign in to Microsoft 365"),
        (
            GraphForbidden("Limit", status=403, code="MaximumTasksInProject"),
            "limits was reached (MaximumTasksInProject)",
        ),
        (GraphForbidden("Forbidden", status=403), "didn't allow that"),
        (GraphNotFound("gone", status=404), "couldn't find that item"),
        (GraphPreconditionFailed("etag", status=412), "changed that in Planner at the same moment"),
        (
            GraphBadRequest("The request is invalid:\r\n  due date before start date", status=400),
            "Planner rejected the request: The request is invalid: due date before start date",
        ),
        (GraphUnavailable("down", status=503), "can't be reached right now"),
        (GraphError("teapot", status=418), "unexpected error"),
    ]
    for error, expected in cases:
        text, unexpected = bot_module.describe_error(error)
        assert expected in text and not unexpected, error
    assert bot_module.describe_error(ValueError("bug")) == ("", True)


@async_test
async def test_graph_problems_reach_the_user_in_plain_words(tmp_path):
    world = make_world()
    bot = make_bot(world, tmp_path)
    world.graph.fail_next(429, headers={"Retry-After": "90"}, times=5)
    interaction = Interaction(bot)

    await invoke(bot_module.task_add, interaction, title="A")

    assert interaction.private_texts == [
        "⚠️ Microsoft Planner is rate limiting requests at the moment. Please try again in about 90 seconds."
    ]
    assert not interaction.anything_public


# --------------------------------------------------------------------------- notifications and start-up


def event(**changes) -> CompletionEvent:
    values = dict(
        task_id="t1",
        title="Write *the* docs",
        url="https://planner.cloud.microsoft/webui/plan/p/view/board/task/t1?tid=x",
        plan_title="Sprint Board",
        bucket="In progress",
        completed_by="Anna Kowalska",
        completed_at=datetime(2026, 10, 5, 16, 2, tzinfo=timezone.utc),
        discord_user_id=None,
    )
    values.update(changes)
    return CompletionEvent(**values)


def test_completion_embed_shows_title_who_bucket_plan_and_link():
    embed = bot_module.completion_embed(event())
    assert (
        plain(embed.title) == "✅ Write *the* docs" and embed.title != "✅ Write *the* docs"
    )  # shown literally, not in italics
    assert embed.url.endswith("/task/t1?tid=x") and "[Open in Planner](" in embed.description
    assert fields(embed) == {"Completed by": "Anna Kowalska", "Bucket": "In progress", "Plan": "Sprint Board"}
    assert embed.timestamp == datetime(2026, 10, 5, 16, 2, tzinfo=timezone.utc)

    sparse = bot_module.completion_embed(event(bucket=None, completed_by=None, plan_title=""))
    assert fields(sparse) == {"Completed by": "Unknown", "Bucket": "No bucket", "Plan": "Unknown"}

    long = bot_module.completion_embed(event(title="x" * 400))
    assert len(long.title) <= 256


@async_test
async def test_delivery_failures_are_classified(tmp_path):
    bot = make_bot(make_world(), tmp_path)

    await bot.deliver_completion(event())
    assert len(bot.channel.sent) == 1

    bot.channel.error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
    with pytest.raises(DeliveryError, match="Embed Links") as forbidden:
        await bot.deliver_completion(event())
    assert forbidden.value.permanent

    bot.channel.error = OSError("network down")
    with pytest.raises(DeliveryError, match="couldn't be reached") as offline:
        await bot.deliver_completion(event())
    assert not offline.value.permanent

    bot.channel = SimpleNamespace(guild=SimpleNamespace(id=GUILD))  # a category, say: nothing can be sent to it
    with pytest.raises(DeliveryError, match="not a channel messages can be sent to"):
        await bot.deliver_completion(event())


@async_test
async def test_start_up_registers_the_commands_in_the_channels_server_and_shuts_down_cleanly(tmp_path):
    world = make_world()
    task_id = world.graph.add_task(world.plan_id, "Write docs")
    bot = make_bot(world, tmp_path, POLL_INTERVAL_SECONDS="15")
    bot.notifier = CompletionNotifier(
        planner=world.planner,
        store=bot.store,
        deliver=bot.deliver_completion,
        now=lambda: world.graph.now,
        interval=15,
        heartbeat_path=bot.config.heartbeat_path,
    )
    synced: list[int] = []

    async def sync(*, guild=None):
        synced.append(guild.id)
        return list(bot_module.COMMAND_GROUPS)

    bot.tree.sync = sync

    await bot.setup_hook()
    await asyncio.sleep(0.2)  # warm-up and the first poll (the baseline) run in the background

    assert synced == [GUILD]
    assert bot.tree.get_command("task", guild=discord.Object(id=GUILD)) is bot_module.task_group
    assert bot.tree.get_command("label", guild=discord.Object(id=GUILD)) is bot_module.label_group
    assert bot.config.db_path.exists() and bot.config.heartbeat_path.exists()
    assert bot.store.counts_sync() == {"tasks": 1}
    assert world.planner._plans.peek("plans") is not None  # caches are warm for the first autocomplete

    await bot.close()
    await bot.close()  # a second call (signal, then context exit) is harmless
    polls = len(world.graph.calls("GET", r"/tasks$"))
    world.graph.complete(task_id)
    await asyncio.sleep(0.1)
    assert len(world.graph.calls("GET", r"/tasks$")) == polls and bot.channel.sent == []


@async_test
async def test_start_up_without_a_visible_channel_reports_instead_of_crashing(tmp_path, caplog):
    world = make_world()
    bot = make_bot(world, tmp_path)

    async def fetch_channel(channel_id):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")

    bot.fetch_channel = fetch_channel
    with caplog.at_level(logging.ERROR):
        await bot.setup_hook()
    await bot.close()

    assert any("Slash commands were NOT registered" in r.getMessage() for r in caplog.records)


def test_due_date_parsing_is_shared_with_the_service():
    assert bot_module.parse_due_date("2026-10-19") == date(2026, 10, 19)
