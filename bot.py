"""Discord side of the Planner bot: slash commands, autocomplete, permissions and messages.

Run it with ``python bot.py``. All behaviour that isn't about Discord lives elsewhere:
planner_service.py (Planner logic), graph_client.py (HTTP), notifier.py and state.py
(completion notifications).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import signal
import sys
import uuid
from typing import Any, Awaitable, Callable, Optional, Sequence, cast

import aiohttp
import discord
from discord import app_commands

from auth import build_token_provider
from config import Config, ConfigError, load_config
from graph_client import (
    GraphAuthError,
    GraphBadRequest,
    GraphClient,
    GraphError,
    GraphForbidden,
    GraphNotFound,
    GraphPreconditionFailed,
    GraphThrottled,
    GraphUnavailable,
)
from notifier import CompletionEvent, CompletionNotifier, DeliveryError
from planner_service import (
    CreatedTask,
    LabelCreated,
    PlannerService,
    TaskListing,
    UserError,
    parse_due_date,
)
from state import StateStore

log = logging.getLogger("planner_bot")

COLOUR_CREATED = 0x5865F2
COLOUR_COMPLETED = 0x2D7D46
COLOUR_LIST = 0x4F6BED
MAX_CHOICES = 25
MAX_CHOICE_LENGTH = 100
# Discord drops an autocomplete request that isn't answered within three seconds.
AUTOCOMPLETE_DEADLINE = 2.5
PUBLIC_DEFER = "planner_public_defer"


# --------------------------------------------------------------------------- formatting


def md(text: str) -> str:
    """Escape Markdown so names and notes show exactly as written."""
    return discord.utils.escape_markdown(text)


def md_title(text: str) -> str:
    """For embed titles: escape only what would really turn into formatting (paired * or _, say)."""
    return discord.utils.escape_markdown(text, as_needed=True)


def link(text: str, url: str) -> str:
    """A Markdown link. Square brackets in the text would end the link early, so they become parentheses."""
    return f"[{md(text.replace('[', '(').replace(']', ')'))}]({url})"


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def created_embed(created: CreatedTask, author_name: str) -> discord.Embed:
    embed = discord.Embed(title=shorten(md_title(created.title), 256), url=created.url, colour=COLOUR_CREATED)
    if created.description:
        embed.description = shorten(md(created.description), 500)
    embed.add_field(name="Bucket", value=md(created.bucket.name) if created.bucket else "No bucket", inline=True)
    embed.add_field(name="Labels", value=shorten(", ".join(md(n) for n in created.labels), 1024) or "None", inline=True)
    embed.add_field(
        name="Assignees",
        value=shorten(", ".join(md(m.label) for m in created.assignees), 1024) or "Unassigned",
        inline=True,
    )
    embed.add_field(
        name="Due date", value=discord.utils.format_dt(created.due, style="D") if created.due else "None", inline=True
    )
    embed.add_field(name="Plan", value=md(created.plan.title), inline=True)
    embed.add_field(name="Link", value=f"[Open in Planner]({created.url})", inline=True)
    embed.set_footer(text=shorten(f"Task created by {author_name}", 2048))
    return embed


def completion_embed(event: CompletionEvent) -> discord.Embed:
    embed = discord.Embed(
        title=shorten(f"✅ {md_title(event.title)}", 256),
        url=event.url,
        description=f"Task completed · [Open in Planner]({event.url})",
        colour=COLOUR_COMPLETED,
        timestamp=event.completed_at,
    )
    if event.discord_user_id is not None:
        who = f"<@{event.discord_user_id}> (via Discord)"
    else:
        who = md(event.completed_by) if event.completed_by else "Unknown"
    embed.add_field(name="Completed by", value=who, inline=True)
    embed.add_field(name="Bucket", value=md(event.bucket) if event.bucket else "No bucket", inline=True)
    embed.add_field(name="Plan", value=md(event.plan_title) if event.plan_title else "Unknown", inline=True)
    embed.set_footer(text="Microsoft Planner")
    return embed


def list_embed(listing: TaskListing) -> discord.Embed:
    kind = "Completed" if listing.completed else "Open"
    embed = discord.Embed(title=shorten(md_title(f"{kind} tasks · {listing.plan.title}"), 256), colour=COLOUR_LIST)
    lines: list[str] = []
    length = 0
    for task in listing.tasks:
        details = []
        if task.bucket:
            details.append(md(task.bucket))
        if listing.completed and task.completed_at:
            details.append(f"done {discord.utils.format_dt(task.completed_at, style='d')}")
        elif task.due:
            details.append(f"due {discord.utils.format_dt(task.due, style='d')}")
        if task.assignees:
            details.append(md(", ".join(task.assignees)))
        if task.labels:
            details.append(md(" / ".join(task.labels)))
        line = "• " + link(shorten(task.title, 100), task.url)
        if details:
            line += " — " + " · ".join(details)
        line = shorten(line, 600)
        if length + len(line) + 1 > 3900:  # an embed description holds 4096 characters
            break
        lines.append(line)
        length += len(line) + 1
    embed.description = "\n".join(lines) if lines else "Nothing to show."

    filters = []
    if listing.bucket:
        filters.append(f"bucket {listing.bucket.name}")
    if listing.assignees:
        filters.append("assigned to " + ", ".join(m.label for m in listing.assignees))
    footer = f"Showing {len(lines)} of {listing.total}" if listing.total > len(lines) else f"{listing.total} task(s)"
    if filters:
        footer += " · " + " · ".join(filters)
    embed.set_footer(text=shorten(footer, 300))  # keeps the whole embed well under Discord's 6000 characters
    return embed


def to_choices(suggestions: Sequence[Any]) -> list[app_commands.Choice[str]]:
    """Turn suggestions into Discord choices. An item is either "text" or ("shown text", "value")."""
    choices: list[app_commands.Choice[str]] = []
    for suggestion in suggestions:
        name, value = (suggestion, suggestion) if isinstance(suggestion, str) else suggestion
        if not name or not value or len(value) > MAX_CHOICE_LENGTH:
            continue
        choices.append(app_commands.Choice(name=shorten(name, MAX_CHOICE_LENGTH), value=value))
        if len(choices) >= MAX_CHOICES:
            break
    return choices


# --------------------------------------------------------------------------- permissions


class NotAllowed(app_commands.CheckFailure):
    """The member has none of the roles that may use the bot."""


def _bot(interaction: discord.Interaction) -> "PlannerBot":
    return cast("PlannerBot", interaction.client)


def is_allowed(interaction: discord.Interaction, allowed_role_ids: frozenset[int]) -> bool:
    """Whether the member holds one of the configured roles.

    @everyone is a role too, and its ID is the server's ID, so listing the server ID in
    ALLOWED_ROLE_IDS opens the bot to all members.
    """
    if not allowed_role_ids or interaction.guild_id is None:
        return False
    held = {interaction.guild_id}
    try:
        held.update(role.id for role in getattr(interaction.user, "roles", None) or [])
    except (AttributeError, TypeError):  # the server isn't in the cache yet
        pass
    return bool(held & allowed_role_ids)


async def require_allowed_role(interaction: discord.Interaction) -> bool:
    if is_allowed(interaction, _bot(interaction).config.allowed_role_ids):
        return True
    raise NotAllowed()


# --------------------------------------------------------------------------- errors


def describe_error(error: BaseException) -> tuple[str, bool]:
    """(what to tell the user, whether the error is unexpected and deserves a stack trace)."""
    if isinstance(error, UserError):
        return str(error), False
    if isinstance(error, NotAllowed):
        return "You don't have a role that is allowed to use this command.", False
    if isinstance(error, app_commands.NoPrivateMessage):
        return "This command only works inside the server.", False
    if isinstance(error, app_commands.CheckFailure):
        return "You can't use this command here.", False
    if isinstance(error, app_commands.TransformerError):
        return "One of the values you entered isn't valid for that option.", False
    if isinstance(error, GraphThrottled):
        seconds = max(int(error.retry_after or 30), 1)
        return (
            f"Microsoft Planner is rate limiting requests at the moment. Please try again in about {seconds} seconds.",
            False,
        )
    if isinstance(error, GraphAuthError):
        return "I can't sign in to Microsoft 365 right now. Whoever runs the bot needs to check its credentials.", False
    if isinstance(error, GraphForbidden):
        if error.code and "maximum" in error.code.lower():
            return f"Planner refused this because one of its limits was reached ({error.code}).", False
        return (
            "Microsoft 365 didn't allow that. The bot may be missing a permission, or it has no access to that plan.",
            False,
        )
    if isinstance(error, GraphNotFound):
        return "Planner couldn't find that item. It may have been deleted. Please try again.", False
    if isinstance(error, GraphPreconditionFailed):
        return "Someone changed that in Planner at the same moment. Please try again.", False
    if isinstance(error, GraphBadRequest):
        reason = shorten(" ".join(error.message.split()), 200)
        return f"Planner rejected the request: {reason}", False
    if isinstance(error, GraphUnavailable):
        return "Microsoft Planner can't be reached right now. Please try again in a minute.", False
    if isinstance(error, GraphError):
        return "Microsoft Planner answered with an unexpected error. Please try again later.", False
    return "", True


async def send_private(interaction: discord.Interaction, text: str) -> None:
    """Show `text` to the invoking user only, whatever state the interaction is in."""
    text = shorten(text, 2000)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(text, ephemeral=True)
        elif interaction.extras.get(PUBLIC_DEFER):
            # The public "thinking…" message can't be turned into a private one. Resolve it,
            # send the details privately, then remove the public leftover.
            await interaction.edit_original_response(content="That didn't work. The details were sent privately.")
            await interaction.followup.send(text, ephemeral=True)
            await interaction.delete_original_response()
        else:
            await interaction.followup.send(text, ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not send a reply to the user", exc_info=True)


async def report_error(interaction: discord.Interaction, error: BaseException) -> None:
    """Log a command failure and tell the user about it in plain words."""
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    command = interaction.command.qualified_name if interaction.command else "?"
    text, unexpected = describe_error(error)
    if unexpected:
        reference = uuid.uuid4().hex[:8]
        log.error("Unexpected error in /%s (reference %s)", command, reference, exc_info=error)
        text = f"Something went wrong on my side. It has been logged (reference `{reference}`)."
    elif isinstance(error, GraphError):
        log.warning("/%s failed: %s", command, error)
    else:
        log.info("/%s was refused: %s", command, error.__class__.__name__)
    await send_private(interaction, f"⚠️ {text}")


class PlannerCommandTree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await report_error(interaction, error)


# --------------------------------------------------------------------------- autocomplete

Suggester = Callable[[discord.Interaction, str], Awaitable[Sequence[Any]]]


def autocompleter(
    suggest: Suggester,
) -> Callable[[discord.Interaction, str], Awaitable[list[app_commands.Choice[str]]]]:
    """Wrap a suggestion function as an autocomplete callback that never fails and never leaks.

    People without an allowed role get no suggestions, so the bot doesn't reveal plans,
    labels or colleagues' names to them. Errors are logged and produce an empty list.
    """

    @functools.wraps(suggest)
    async def callback(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not is_allowed(interaction, _bot(interaction).config.allowed_role_ids):
            return []
        try:
            return to_choices(await asyncio.wait_for(suggest(interaction, current), AUTOCOMPLETE_DEADLINE))
        except asyncio.TimeoutError:
            return []  # Graph is slow; what was requested keeps loading for the next keystroke
        except Exception:
            log.warning("Autocomplete %s failed", suggest.__name__, exc_info=True)
            return []

    return callback


def _chosen_plan(interaction: discord.Interaction) -> Optional[str]:
    """The plan option as filled in so far, which decides whose buckets, labels and tasks are suggested."""
    value = getattr(interaction.namespace, "plan", None)
    return value if isinstance(value, str) else None


@autocompleter
async def plan_autocomplete(interaction: discord.Interaction, current: str) -> Sequence[Any]:
    return await _bot(interaction).planner.suggest_plans(current)


@autocompleter
async def bucket_autocomplete(interaction: discord.Interaction, current: str) -> Sequence[Any]:
    return await _bot(interaction).planner.suggest_buckets(_chosen_plan(interaction), current)


@autocompleter
async def labels_autocomplete(interaction: discord.Interaction, current: str) -> Sequence[Any]:
    return await _bot(interaction).planner.suggest_labels(_chosen_plan(interaction), current)


@autocompleter
async def assignee_autocomplete(interaction: discord.Interaction, current: str) -> Sequence[Any]:
    return await _bot(interaction).planner.suggest_assignees(current)


@autocompleter
async def task_autocomplete(interaction: discord.Interaction, current: str) -> Sequence[Any]:
    return await _bot(interaction).planner.suggest_tasks(_chosen_plan(interaction), current)


# --------------------------------------------------------------------------- commands

task_group = app_commands.Group(name="task", description="Create and manage Microsoft Planner tasks", guild_only=True)
label_group = app_commands.Group(name="label", description="Manage the labels of a Planner plan", guild_only=True)


@task_group.command(name="add", description="Create a task in Microsoft Planner")
@app_commands.describe(
    title="Title of the task",
    plan="Plan to add the task to (default plan if left empty)",
    bucket="Bucket in that plan (default bucket if left empty)",
    labels="One or more label names, separated by commas",
    assignee="One or more people, by name or email, separated by commas",
    due_date="Due date as YYYY-MM-DD",
    description="Notes for the task",
)
@app_commands.autocomplete(
    plan=plan_autocomplete, bucket=bucket_autocomplete, labels=labels_autocomplete, assignee=assignee_autocomplete
)
@app_commands.check(require_allowed_role)
async def task_add(
    interaction: discord.Interaction,
    title: app_commands.Range[str, 1, 255],
    plan: Optional[str] = None,
    bucket: Optional[str] = None,
    labels: Optional[str] = None,
    assignee: Optional[str] = None,
    due_date: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    due = parse_due_date(due_date)  # checked before anything is sent, so a typo gets a private reply
    interaction.extras[PUBLIC_DEFER] = True
    await interaction.response.defer(thinking=True)
    created = await _bot(interaction).planner.create_task(
        title=title, plan=plan, bucket=bucket, labels=labels, assignees=assignee, due=due, description=description
    )
    await interaction.edit_original_response(embed=created_embed(created, interaction.user.display_name))
    for warning in created.warnings:
        await interaction.followup.send(f"⚠️ {warning}", ephemeral=True)


@task_group.command(name="list", description="Show the tasks of a plan")
@app_commands.describe(
    plan="Plan to show (default plan if left empty)",
    bucket="Only tasks in this bucket",
    assignee="Only tasks assigned to these people (names or emails, separated by commas)",
    completed="Show completed tasks instead of open ones",
)
@app_commands.autocomplete(plan=plan_autocomplete, bucket=bucket_autocomplete, assignee=assignee_autocomplete)
@app_commands.check(require_allowed_role)
async def task_list(
    interaction: discord.Interaction,
    plan: Optional[str] = None,
    bucket: Optional[str] = None,
    assignee: Optional[str] = None,
    completed: bool = False,
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    listing = await _bot(interaction).planner.list_tasks(
        plan=plan, bucket=bucket, assignee=assignee, completed=completed
    )
    await interaction.followup.send(embed=list_embed(listing), ephemeral=True)


@task_group.command(name="complete", description="Mark a Planner task as completed")
@app_commands.describe(
    task="Start typing the task's title and pick it from the list",
    plan="Plan the task is in (default plan if left empty)",
)
@app_commands.autocomplete(task=task_autocomplete, plan=plan_autocomplete)
@app_commands.check(require_allowed_role)
async def task_complete(interaction: discord.Interaction, task: str, plan: Optional[str] = None) -> None:
    bot = _bot(interaction)
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await bot.planner.complete_task(task, plan=plan)
    task_link = link(shorten(result.summary.title, 200), result.summary.url)
    if not result.changed:
        await interaction.followup.send(f"{task_link} was already completed.", ephemeral=True)
        return
    await bot.notifier.record_bot_completion(
        result.task, plan_title=result.plan.title, discord_user_id=interaction.user.id
    )
    await interaction.followup.send(
        f"✅ Marked {task_link} as completed. The notification goes to <#{bot.config.notify_channel_id}>.",
        ephemeral=True,
    )


@label_group.command(name="create", description="Give a name to an unused label of a plan")
@app_commands.describe(
    name="Name for the new label",
    plan="Plan to add the label to (default plan if left empty)",
)
@app_commands.autocomplete(plan=plan_autocomplete)
@app_commands.check(require_allowed_role)
async def label_create(
    interaction: discord.Interaction, name: app_commands.Range[str, 1, 100], plan: Optional[str] = None
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    created = await _bot(interaction).planner.create_label(name, plan=plan)
    await interaction.followup.send(label_created_text(created), ephemeral=True)


def label_created_text(created: LabelCreated) -> str:
    text = f"🏷️ Label **{md(created.name)}** now exists in plan **{md(created.plan.title)}**. Use it with `/task add`."
    if created.tasks_already_using_slot:
        text += (
            f"\nNote: every unnamed label was already in use, so this one took a colour that "
            f"{created.tasks_already_using_slot} task(s) carry. Those tasks now show the new name."
        )
    return text


COMMAND_GROUPS = (task_group, label_group)


# --------------------------------------------------------------------------- the bot


class PlannerBot(discord.Client):
    def __init__(self, config: Config) -> None:
        # The guilds intent is all that slash commands and posting to a channel need.
        super().__init__(intents=discord.Intents(guilds=True), allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.tree = PlannerCommandTree(self)
        self.graph = GraphClient(build_token_provider(config))
        self.planner = PlannerService(
            self.graph,
            default_plan_id=config.default_plan_id,
            tenant_id=config.tenant_id,
            group_id=config.group_id,
            default_bucket=config.default_bucket,
            cache_ttl=config.cache_ttl,
            task_url_template=config.task_url_template,
        )
        self.store = StateStore(config.db_path)
        self.notifier = CompletionNotifier(
            planner=self.planner,
            store=self.store,
            deliver=self.deliver_completion,
            interval=config.poll_interval,
            heartbeat_path=config.heartbeat_path,
        )
        self._warm_up_task: Optional[asyncio.Task[None]] = None
        self._shutdown_task: Optional[asyncio.Future[None]] = None

    async def setup_hook(self) -> None:
        """Runs once after logging in and before the gateway connection is opened."""
        await self.graph.start()
        await self.store.aopen()
        for group in COMMAND_GROUPS:
            self.tree.add_command(group)
        await self._register_commands()
        self._warm_up_task = asyncio.create_task(self._warm_up(), name="planner-warm-up")
        self.notifier.start()

    async def on_ready(self) -> None:
        log.info("Connected to Discord as %s", self.user)

    async def close(self) -> None:
        # close() can be called more than once (a signal, then the end of `async with`).
        # Every caller waits for the same shutdown to finish.
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.ensure_future(self._shutdown())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        if self._warm_up_task is not None:
            self._warm_up_task.cancel()
        await self.notifier.stop()
        await self.graph.close()
        await self.store.aclose()
        await super().close()

    async def _register_commands(self) -> None:
        """Register the slash commands in the bot's server, where they appear immediately."""
        guild_id = self.config.guild_id
        if guild_id is None:
            # Not configured: use the server that owns the notification channel.
            try:
                channel = await self.fetch_channel(self.config.notify_channel_id)
            except (discord.HTTPException, discord.InvalidData) as exc:
                log.error(
                    "Can't see the channel in NOTIFY_CHANNEL_ID (%s): %s. Is the bot in that server?",
                    self.config.notify_channel_id,
                    exc,
                )
            else:
                guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if guild_id is None:
            log.error(
                "Slash commands were NOT registered. Fix NOTIFY_CHANNEL_ID or set DISCORD_GUILD_ID, then restart."
            )
            return
        guild = discord.Object(id=guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            log.error(
                "Registering the slash commands in server %s failed: %s. "
                "Invite the bot with both the 'bot' and 'applications.commands' scopes.",
                guild_id,
                exc,
            )
            return
        log.info("Registered %d command group(s) in server %s", len(synced), guild_id)

    async def _warm_up(self) -> None:
        """Fill the caches so the first autocomplete is fast, and surface setup problems early."""
        try:
            await self.planner.keep_warm()
        except (GraphError, UserError) as exc:
            log.error("Microsoft Planner isn't reachable with the current settings: %s", exc)
        except Exception:
            log.exception("Unexpected error while loading Planner data")
        else:
            plans = await self.planner.plans()
            log.info("Microsoft Planner is reachable: %d plan(s) in the group", len(plans))

    async def deliver_completion(self, event: CompletionEvent) -> None:
        """Post a completion notification to the configured channel."""
        await self.wait_until_ready()
        channel_id = self.config.notify_channel_id
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            send = getattr(channel, "send", None)
            if send is None:
                raise DeliveryError(
                    f"NOTIFY_CHANNEL_ID ({channel_id}) is not a channel messages can be sent to", permanent=True
                )
            await send(embed=completion_embed(event))
        except discord.Forbidden as exc:
            raise DeliveryError(
                f"the bot may not post in channel {channel_id} (it needs View Channel, Send Messages and Embed Links)",
                permanent=True,
            ) from exc
        except discord.NotFound as exc:
            raise DeliveryError(
                f"channel {channel_id} (NOTIFY_CHANNEL_ID) doesn't exist or the bot isn't in that server",
                permanent=True,
            ) from exc
        except discord.HTTPException as exc:
            raise DeliveryError(f"Discord answered with an error ({exc})") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise DeliveryError(f"Discord couldn't be reached ({exc.__class__.__name__})") from exc


# --------------------------------------------------------------------------- entry point


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("msal").setLevel(logging.WARNING)
    if level != "DEBUG":
        logging.getLogger("discord").setLevel(logging.WARNING)


async def run(config: Config) -> None:
    bot = PlannerBot(config)
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            # `docker stop` sends SIGTERM: shut down cleanly instead of being killed.
            loop.add_signal_handler(signal_number, lambda: asyncio.ensure_future(bot.close()))
        except NotImplementedError:  # Windows
            pass
    async with bot:
        await bot.start(config.discord_token)


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    setup_logging(config.log_level)
    for warning in config.warnings:
        log.warning(warning)
    try:
        asyncio.run(run(config))
    except discord.LoginFailure:
        log.error("Discord rejected DISCORD_TOKEN. Copy the bot token again from the developer portal.")
        return 1
    except (aiohttp.ClientError, OSError) as exc:
        log.error("Could not reach Discord: %s", exc)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
