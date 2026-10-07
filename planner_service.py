"""Planner operations for the bot, expressed in names instead of IDs.

This module knows how Planner is laid out in Microsoft Graph (plans belong to a group,
buckets and label names belong to a plan, labels are the fixed slots category1..category25)
and turns what people type in Discord into the IDs Graph wants. It also keeps short-lived
caches of plans, buckets, labels, group members and tasks, so that autocomplete can answer
from memory within Discord's three-second limit.

Nothing here imports discord: the Discord side lives in bot.py.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Sequence, TypeVar
from urllib.parse import quote

from cache import TTLCache
from config import DEFAULT_TASK_URL_TEMPLATE
from graph_client import (
    GraphBadRequest,
    GraphClient,
    GraphError,
    GraphForbidden,
    GraphNotFound,
    GraphPreconditionFailed,
)
from matching import ListCandidate, did_you_mean, listing, name_key, rank_matches, split_names, suggest_list

log = logging.getLogger(__name__)

T = TypeVar("T")

LABEL_SLOTS = tuple(f"category{number}" for number in range(1, 26))
MAX_TITLE_LENGTH = 255
PLANNER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,40}$")
# Date-only due dates are stored at 10:00 UTC, which is the same calendar day everywhere
# from UTC-10 to UTC+13.
DUE_TIME_UTC = clock_time(10, 0)
ETAG_ATTEMPTS = 4

_PLANS = "plans"
_MEMBERS = "members"
# A name is looked up in the cache first. If it isn't there, the list is fetched again
# (unless that happened within the last few seconds) before the name is declared unknown,
# because the bucket, label or member may simply be newer than the cache.
_RECENT = 5.0
_LOOKUP_PASSES: tuple[Optional[float], ...] = (None, _RECENT)
_FRACTION_RE = re.compile(r"\.(\d+)")


class UserError(Exception):
    """Something the person running the command can understand and fix. The text is shown to them."""


class SetupError(UserError):
    """The bot is set up incorrectly. The text tells whoever runs it what to change."""


# --------------------------------------------------------------------------- dates and small helpers


def parse_graph_datetime(value: Any) -> Optional[datetime]:
    """Parse a Graph timestamp such as 2026-10-05T16:02:11.1234567Z (note the seven decimals)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in "zZ":
        text = text[:-1] + "+00:00"
    text = _FRACTION_RE.sub(lambda match: "." + match.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_graph_datetime(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_due_date(text: Optional[str]) -> Optional[date]:
    """Parse a YYYY-MM-DD due date typed by a user. Empty means no due date."""
    if text is None or not text.strip():
        return None
    text = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    example = (date.today() + timedelta(days=7)).isoformat()
    raise UserError(f"“{text}” isn't a valid due date. Use the format YYYY-MM-DD, for example {example}.")


def due_moment(day: date) -> datetime:
    return datetime.combine(day, DUE_TIME_UTC, tzinfo=timezone.utc)


def assignee_ids(task: dict[str, Any]) -> list[str]:
    """User IDs a task is assigned to. `assignments` is keyed by user ID; OData annotations are skipped."""
    assignments = task.get("assignments") or {}
    return [key for key, value in assignments.items() if not key.startswith("@") and value is not None]


def _seg(value: str) -> str:
    """Encode a value for use as one URL path segment."""
    return quote(str(value), safe="")


async def _checked(lookup: Awaitable[T]) -> tuple[Optional[T], Optional[str]]:
    """Await a lookup. A mistake in what the user typed comes back as text instead of an exception."""
    try:
        return await lookup, None
    except UserError as mistake:
        return None, str(mistake)


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Plan:
    id: str
    title: str


@dataclass(frozen=True)
class Bucket:
    id: str
    name: str
    order_hint: str = ""


@dataclass(frozen=True)
class Member:
    id: str
    display_name: str
    email: str = ""  # mail, or the user principal name when there is no mailbox
    upn: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.email or self.id


@dataclass(frozen=True)
class PlanLabels:
    """The named label slots of a plan: {"category3": "Bug", ...}."""

    names: dict[str, str]

    @classmethod
    def from_details(cls, details: dict[str, Any]) -> "PlanLabels":
        descriptions = details.get("categoryDescriptions") or {}
        named = {}
        for slot in LABEL_SLOTS:
            value = descriptions.get(slot)
            if isinstance(value, str) and value.strip():
                named[slot] = value.strip()
        return cls(names=named)

    def slot_for(self, name: str) -> Optional[str]:
        wanted = name_key(name)
        for slot, label in self.names.items():
            if name_key(label) == wanted:
                return slot
        return None


@dataclass(frozen=True)
class CreatedTask:
    id: str
    title: str
    plan: Plan
    bucket: Optional[Bucket]
    labels: tuple[str, ...]
    assignees: tuple[Member, ...]
    due: Optional[datetime]
    description: Optional[str]
    url: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskSummary:
    id: str
    title: str
    bucket: Optional[str]
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    due: Optional[datetime]
    completed_at: Optional[datetime]
    percent_complete: int
    url: str


@dataclass(frozen=True)
class TaskListing:
    plan: Plan
    tasks: tuple[TaskSummary, ...]
    total: int
    completed: bool
    bucket: Optional[Bucket] = None
    assignees: tuple[Member, ...] = ()


@dataclass(frozen=True)
class CompletionResult:
    task: dict[str, Any]  # the task as Graph returned it after the update
    plan: Plan
    summary: TaskSummary
    changed: bool  # False when the task was already complete


@dataclass(frozen=True)
class LabelCreated:
    plan: Plan
    slot: str
    name: str
    tasks_already_using_slot: int


class _MemberIndex:
    """Looks group members up by display name, email, user principal name or ID."""

    def __init__(self, members: Sequence[Member]) -> None:
        self.members = members
        self._by_name: dict[str, list[Member]] = {}
        self._by_address: dict[str, Member] = {}
        for member in members:
            if member.display_name:
                self._by_name.setdefault(name_key(member.display_name), []).append(member)
            for address in (member.email, member.upn, member.id):
                if address:
                    self._by_address.setdefault(name_key(address), member)

    @property
    def vocabulary(self) -> list[str]:
        return [m.display_name for m in self.members if m.display_name]

    def find(self, text: str) -> list[Member]:
        key = name_key(text)
        if key in self._by_address:
            return [self._by_address[key]]
        return list(self._by_name.get(key, []))

    def token_for(self, member: Member) -> str:
        """How this member is written in a list: the display name unless that is ambiguous."""
        if member.display_name and len(self._by_name.get(name_key(member.display_name), [])) == 1:
            return member.display_name
        return member.email or member.upn or member.id

    def candidates(self) -> list[ListCandidate]:
        return [
            ListCandidate(token=self.token_for(m), keys=tuple(k for k in (m.display_name, m.email, m.upn) if k))
            for m in self.members
        ]


# --------------------------------------------------------------------------- service


class PlannerService:
    FAST_TIMEOUT = 2.0  # how long a suggestion may wait for Graph when nothing is cached yet

    def __init__(
        self,
        graph: GraphClient,
        *,
        default_plan_id: str,
        tenant_id: str,
        group_id: Optional[str] = None,
        default_bucket: Optional[str] = None,
        cache_ttl: float = 300.0,
        tasks_ttl: float = 20.0,
        task_url_template: str = DEFAULT_TASK_URL_TEMPLATE,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._graph = graph
        self.default_plan_id = default_plan_id
        self._tenant_id = tenant_id
        self._group_id = group_id
        self._group_lock = asyncio.Lock()
        self._default_bucket = default_bucket
        self._task_url_template = task_url_template
        self._sleep = sleep
        self._plans: TTLCache[str, list[Plan]] = TTLCache(cache_ttl, name="plans")
        self._buckets: TTLCache[str, list[Bucket]] = TTLCache(cache_ttl, name="buckets")
        self._labels: TTLCache[str, PlanLabels] = TTLCache(cache_ttl, name="labels")
        self._members: TTLCache[str, list[Member]] = TTLCache(cache_ttl, name="members")
        self._tasks: TTLCache[str, list[dict[str, Any]]] = TTLCache(tasks_ttl, name="tasks")
        self._user_names: dict[str, tuple[Optional[str], float]] = {}
        self._warned_nameless_members = False

    # ------------------------------------------------------------------ loading

    async def group_id(self) -> str:
        """The Microsoft 365 group whose plans the bot works with."""
        if self._group_id is None:
            async with self._group_lock:
                if self._group_id is None:
                    self._group_id = await self._group_of_default_plan()
        return self._group_id

    async def _group_of_default_plan(self) -> str:
        try:
            plan = await self._graph.get(f"/planner/plans/{_seg(self.default_plan_id)}")
        except (GraphNotFound, GraphBadRequest) as exc:
            raise SetupError(
                "The plan in DEFAULT_PLAN_ID wasn't found. Check the ID; note that Premium plans "
                "can't be reached through Microsoft Graph."
            ) from exc
        container = plan.get("container") or {}
        kind = str(container.get("type") or "group").lower()
        group = container.get("containerId") or plan.get("owner")
        if kind != "group" or not group:
            raise SetupError(
                f"The plan in DEFAULT_PLAN_ID isn't owned by a Microsoft 365 group (it is a “{kind}” plan). "
                "Use a plan that lives in a group or team."
            )
        return str(group)

    async def _load_plans(self) -> list[Plan]:
        group = await self.group_id()
        rows = await self._graph.get_all(f"/groups/{_seg(group)}/planner/plans")
        plans = [
            Plan(id=str(row["id"]), title=str(row.get("title") or "Untitled plan")) for row in rows if row.get("id")
        ]
        plans.sort(key=lambda plan: (plan.id != self.default_plan_id, plan.title.casefold()))
        return plans

    async def _load_buckets(self, plan_id: str) -> list[Bucket]:
        rows = await self._graph.get_all(f"/planner/plans/{_seg(plan_id)}/buckets")
        buckets = [
            Bucket(
                id=str(row["id"]),
                name=str(row.get("name") or "Unnamed bucket"),
                order_hint=str(row.get("orderHint") or ""),
            )
            for row in rows
            if row.get("id")
        ]
        # Planner orders items by comparing order hints character by character.
        buckets.sort(key=lambda bucket: bucket.order_hint)
        return buckets

    async def _load_labels(self, plan_id: str) -> PlanLabels:
        details = await self._graph.get(f"/planner/plans/{_seg(plan_id)}/details")
        return PlanLabels.from_details(details)

    async def _load_members(self) -> list[Member]:
        path = f"/groups/{_seg(await self.group_id())}/members"
        try:
            rows = await self._graph.get_all(
                path, params={"$select": "id,displayName,mail,userPrincipalName", "$top": "999"}
            )
        except GraphBadRequest:
            # Graph documents query options on this endpoint as an "advanced query" feature.
            # Should it refuse them, the plain listing returns the same properties, 100 at a time.
            rows = await self._graph.get_all(path)
        members: list[Member] = []
        for row in rows:
            kind = row.get("@odata.type")
            if (kind and kind != "#microsoft.graph.user") or not row.get("id"):
                continue
            mail = str(row.get("mail") or "").strip()
            upn = str(row.get("userPrincipalName") or "").strip()
            members.append(
                Member(
                    id=str(row["id"]),
                    display_name=str(row.get("displayName") or "").strip(),
                    email=mail or upn,
                    upn=upn,
                )
            )
        if members and not any(m.display_name or m.email for m in members) and not self._warned_nameless_members:
            # Graph hides the properties of member types the app may not read.
            self._warned_nameless_members = True
            log.warning(
                "Graph returned the group's members without names or emails, so assignees can't be "
                "looked up by name. Grant User.ReadBasic.All (or User.Read.All) and admin consent."
            )
        members.sort(key=lambda member: member.label.casefold())
        return members

    async def _load_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        return await self._graph.get_all(f"/planner/plans/{_seg(plan_id)}/tasks")

    # Each getter returns cached data unless it is older than the cache's TTL, or older
    # than `max_age` seconds when the caller needs something more recent.

    async def plans(self, *, max_age: Optional[float] = None) -> list[Plan]:
        return await self._plans.get(_PLANS, self._load_plans, max_age=max_age)

    async def buckets(self, plan_id: str, *, max_age: Optional[float] = None) -> list[Bucket]:
        return await self._buckets.get(plan_id, lambda: self._load_buckets(plan_id), max_age=max_age)

    async def labels(self, plan_id: str, *, max_age: Optional[float] = None) -> PlanLabels:
        return await self._labels.get(plan_id, lambda: self._load_labels(plan_id), max_age=max_age)

    async def members(self, *, max_age: Optional[float] = None) -> list[Member]:
        return await self._members.get(_MEMBERS, self._load_members, max_age=max_age)

    async def tasks(self, plan_id: str, *, max_age: Optional[float] = None) -> list[dict[str, Any]]:
        return await self._tasks.get(plan_id, lambda: self._load_tasks(plan_id), max_age=max_age)

    async def keep_warm(self) -> None:
        """Refresh whatever has expired, so autocomplete rarely has to wait for Graph."""
        plans = await self.plans()
        await self.members()
        for plan in plans:
            await self.buckets(plan.id)
            await self.labels(plan.id)

    def task_url(self, plan_id: str, task_id: str) -> str:
        return self._task_url_template.format(
            plan_id=_seg(plan_id), task_id=_seg(task_id), tenant_id=_seg(self._tenant_id)
        )

    # ------------------------------------------------------------------ names -> IDs

    async def resolve_plan(self, text: Optional[str]) -> Plan:
        """The plan a user named (by title or ID), or the default plan when they named none."""
        wanted = (text or "").strip()
        plans: list[Plan] = []
        for max_age in _LOOKUP_PASSES:
            plans = await self.plans(max_age=max_age)
            if wanted:
                matches = [p for p in plans if p.id == wanted] or [
                    p for p in plans if name_key(p.title) == name_key(wanted)
                ]
            else:
                matches = [p for p in plans if p.id == self.default_plan_id]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise UserError(
                    f"Several plans are called “{wanted}”. Pick one from the suggestions instead of typing the name."
                )
        if not wanted:
            raise SetupError(
                "The plan in DEFAULT_PLAN_ID isn't one of the plans of the configured group. "
                "Check DEFAULT_PLAN_ID and GROUP_ID."
            )
        titles = [p.title for p in plans]
        raise UserError(f"There's no plan called “{wanted}”.{did_you_mean(wanted, titles)} Plans: {listing(titles)}.")

    async def resolve_bucket(self, plan: Plan, text: Optional[str]) -> Optional[Bucket]:
        """The bucket a user named, or the default: DEFAULT_BUCKET if this plan has it, else the first bucket."""
        wanted = (text or "").strip()
        explicit = bool(wanted)
        if not explicit:
            wanted = self._default_bucket or ""
        buckets: list[Bucket] = []
        for max_age in _LOOKUP_PASSES:
            buckets = await self.buckets(plan.id, max_age=max_age)
            if not wanted:
                break
            matches = [b for b in buckets if b.id == wanted] or [
                b for b in buckets if name_key(b.name) == name_key(wanted)
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                if explicit:
                    raise UserError(
                        f"Plan “{plan.title}” has several buckets called “{wanted}”. Pick one from the suggestions."
                    )
                return matches[0]
            if not explicit:
                break  # the configured default simply isn't part of this plan
        if explicit:
            names = [b.name for b in buckets]
            raise UserError(
                f"There's no bucket called “{wanted}” in plan “{plan.title}”.{did_you_mean(wanted, names)} "
                f"Buckets: {listing(names) or 'none yet'}."
            )
        return buckets[0] if buckets else None

    async def resolve_labels(self, plan: Plan, text: Optional[str]) -> list[tuple[str, str]]:
        """[(slot, name)] for the label names a user typed. Unknown names are an error."""
        if text is None or not text.strip():
            return []
        labels = PlanLabels(names={})
        wanted: list[str] = []
        missing: list[str] = []
        for max_age in _LOOKUP_PASSES:
            labels = await self.labels(plan.id, max_age=max_age)
            wanted = split_names(text, labels.names.values())
            missing = [name for name in wanted if labels.slot_for(name) is None]
            if not missing:
                break
        if missing:
            available = list(labels.names.values())
            if not available:
                raise UserError(
                    f"Plan “{plan.title}” has no named labels yet. "
                    "Create one with /label create, or name a label in Planner."
                )
            problems = [
                f"Label “{name}” doesn't exist in plan “{plan.title}”.{did_you_mean(name, available)}"
                for name in missing
            ]
            raise UserError(
                " ".join(problems) + f" Available labels: {listing(available, 25)}. Use /label create to add one."
            )
        resolved: dict[str, str] = {}
        for name in wanted:
            slot = labels.slot_for(name)
            assert slot is not None
            resolved.setdefault(slot, labels.names[slot])
        return list(resolved.items())

    async def resolve_assignees(self, text: Optional[str]) -> list[Member]:
        """Group members for the names or emails a user typed."""
        if text is None or not text.strip():
            return []
        index = _MemberIndex([])
        found: dict[str, Member] = {}
        missing: list[str] = []
        ambiguous: list[tuple[str, list[Member]]] = []
        for max_age in _LOOKUP_PASSES:
            index = _MemberIndex(await self.members(max_age=max_age))
            found, missing, ambiguous = {}, [], []
            for name in split_names(text, index.vocabulary):
                matches = index.find(name)
                if len(matches) == 1:
                    found.setdefault(matches[0].id, matches[0])
                elif matches:
                    ambiguous.append((name, matches))
                else:
                    missing.append(name)
            if not missing:
                break
        problems: list[str] = []
        for name in missing:
            hint = did_you_mean(name, [m.display_name for m in index.members] + [m.email for m in index.members])
            problems.append(f"Nobody in the plan's group is called “{name}”.{hint}")
        for name, matches in ambiguous:
            emails = listing(m.email or m.id for m in matches)
            problems.append(f"“{name}” matches {len(matches)} people. Use an email instead: {emails}.")
        if problems:
            if missing:
                problems.append("Start typing in the assignee option to see who can be assigned.")
            raise UserError(" ".join(problems))
        return list(found.values())

    # ------------------------------------------------------------------ autocomplete

    async def suggest_plans(self, current: str) -> list[tuple[str, str]]:
        plans = await self._plans.get_fast(_PLANS, self._load_plans, wait=self.FAST_TIMEOUT)
        return [(plan.title, plan.id) for plan in rank_matches(current, plans or [], lambda p: (p.title,))]

    async def _plan_fast(self, text: Optional[str]) -> Optional[Plan]:
        plans = await self._plans.get_fast(_PLANS, self._load_plans, wait=self.FAST_TIMEOUT)
        wanted = (text or "").strip() or self.default_plan_id
        for plan in plans or []:
            if plan.id == wanted or name_key(plan.title) == name_key(wanted):
                return plan
        return None

    async def suggest_buckets(self, plan_text: Optional[str], current: str) -> list[tuple[str, str]]:
        plan = await self._plan_fast(plan_text)
        if plan is None:
            return []
        buckets = await self._buckets.get_fast(plan.id, lambda: self._load_buckets(plan.id), wait=self.FAST_TIMEOUT)
        return [(bucket.name, bucket.id) for bucket in rank_matches(current, buckets or [], lambda b: (b.name,))]

    async def suggest_labels(self, plan_text: Optional[str], current: str) -> list[str]:
        plan = await self._plan_fast(plan_text)
        if plan is None:
            return []
        labels = await self._labels.get_fast(plan.id, lambda: self._load_labels(plan.id), wait=self.FAST_TIMEOUT)
        if labels is None:
            return []
        return suggest_list(current, [ListCandidate(token=name) for name in labels.names.values()])

    async def suggest_assignees(self, current: str) -> list[str]:
        members = await self._members.get_fast(_MEMBERS, self._load_members, wait=self.FAST_TIMEOUT)
        return suggest_list(current, _MemberIndex(members or []).candidates())

    async def suggest_tasks(self, plan_text: Optional[str], current: str) -> list[tuple[str, str]]:
        """Open tasks of the plan, for /task complete: (text to show, task ID)."""
        plan = await self._plan_fast(plan_text)
        if plan is None:
            return []
        tasks = await self._tasks.get_fast(plan.id, lambda: self._load_tasks(plan.id), wait=self.FAST_TIMEOUT)
        buckets = self._buckets.peek(plan.id) or []
        bucket_names = {bucket.id: bucket.name for bucket in buckets}
        open_tasks = [t for t in tasks or [] if t.get("id") and int(t.get("percentComplete") or 0) < 100]
        suggestions = []
        for task in rank_matches(current, open_tasks, lambda t: (str(t.get("title") or ""),)):
            bucket = bucket_names.get(str(task.get("bucketId") or ""))
            title = str(task.get("title") or "Untitled task")
            suggestions.append((f"{title} · {bucket}" if bucket else title, str(task["id"])))
        return suggestions

    # ------------------------------------------------------------------ creating tasks

    async def create_task(
        self,
        *,
        title: str,
        plan: Optional[str] = None,
        bucket: Optional[str] = None,
        labels: Optional[str] = None,
        assignees: Optional[str] = None,
        due: Optional[date] = None,
        description: Optional[str] = None,
    ) -> CreatedTask:
        title = " ".join(title.split())
        if not title:
            raise UserError("The task needs a title.")
        if len(title) > MAX_TITLE_LENGTH:
            raise UserError(
                f"The title is too long for Planner ({len(title)} characters, the limit is {MAX_TITLE_LENGTH})."
            )
        description = (description or "").strip() or None

        plan_obj = await self.resolve_plan(plan)
        # Look everything up before creating anything, and report all mistakes at once.
        (bucket_obj, bucket_mistake), (label_slots, label_mistake), (members, assignee_mistake) = await asyncio.gather(
            _checked(self.resolve_bucket(plan_obj, bucket)),
            _checked(self.resolve_labels(plan_obj, labels)),
            _checked(self.resolve_assignees(assignees)),
        )
        mistakes = [mistake for mistake in (bucket_mistake, label_mistake, assignee_mistake) if mistake]
        if mistakes:
            raise UserError("\n".join(mistakes))
        label_slots = label_slots or []
        members = members or []

        body: dict[str, Any] = {"planId": plan_obj.id, "title": title}
        if bucket_obj is not None:
            body["bucketId"] = bucket_obj.id
        if label_slots:
            body["appliedCategories"] = {slot: True for slot, _ in label_slots}
        if members:
            body["assignments"] = {
                member.id: {"@odata.type": "#microsoft.graph.plannerAssignment", "orderHint": " !"}
                for member in members
            }
        due_at = due_moment(due) if due is not None else None
        if due_at is not None:
            body["dueDateTime"] = to_graph_datetime(due_at)

        try:
            task = await self._graph.post("/planner/tasks", json=body)
        except (GraphBadRequest, GraphNotFound):
            # A bucket or label may have been removed since it was cached; start clean next time.
            self._buckets.invalidate(plan_obj.id)
            self._labels.invalidate(plan_obj.id)
            raise
        task_id = str(task["id"])
        self._remember_task(task)
        log.info("Created task %s in plan %s (%s)", task_id, plan_obj.id, plan_obj.title)

        warnings: list[str] = []
        if description:
            try:
                await self._set_description(task_id, description)
            except GraphError as exc:
                log.warning("Task %s was created but its description could not be saved: %s", task_id, exc)
                warnings.append(
                    "The task was created, but its description couldn't be saved. Please add it in Planner."
                )

        return CreatedTask(
            id=task_id,
            title=title,
            plan=plan_obj,
            bucket=bucket_obj,
            labels=tuple(name for _, name in label_slots),
            assignees=tuple(members),
            due=due_at,
            description=description,
            url=self.task_url(plan_obj.id, task_id),
            warnings=tuple(warnings),
        )

    async def _set_description(self, task_id: str, description: str) -> None:
        """The description lives in the task's details object, which has its own ETag."""
        path = f"/planner/tasks/{_seg(task_id)}/details"
        attempts = 5
        for attempt in range(1, attempts + 1):
            try:
                details = await self._graph.get(path)
                await self._graph.patch(path, json={"description": description}, etag=details["@odata.etag"])
                return
            except (GraphNotFound, GraphPreconditionFailed):
                # Right after creation the details can be unreadable for a moment (404),
                # and Planner itself may touch them, changing the ETag (412).
                if attempt == attempts:
                    raise
                await self._sleep(0.4 * 2 ** (attempt - 1))

    # ------------------------------------------------------------------ listing and completing

    async def list_tasks(
        self,
        *,
        plan: Optional[str] = None,
        bucket: Optional[str] = None,
        assignee: Optional[str] = None,
        completed: bool = False,
        limit: int = 20,
    ) -> TaskListing:
        plan_obj = await self.resolve_plan(plan)
        bucket_obj = await self.resolve_bucket(plan_obj, bucket) if bucket and bucket.strip() else None
        people = await self.resolve_assignees(assignee)
        person_ids = {member.id for member in people}

        selected = []
        # A listing should show what is in Planner now, not what the last poll saw.
        for task in await self.tasks(plan_obj.id, max_age=_RECENT):
            if (int(task.get("percentComplete") or 0) >= 100) != completed:
                continue
            if bucket_obj is not None and task.get("bucketId") != bucket_obj.id:
                continue
            if person_ids and not person_ids & set(assignee_ids(task)):
                continue
            selected.append(task)

        far_future = datetime.max.replace(tzinfo=timezone.utc)
        long_ago = datetime.min.replace(tzinfo=timezone.utc)
        if completed:
            selected.sort(key=lambda t: parse_graph_datetime(t.get("completedDateTime")) or long_ago, reverse=True)
        else:
            selected.sort(
                key=lambda t: (
                    parse_graph_datetime(t.get("dueDateTime")) or far_future,
                    str(t.get("title") or "").casefold(),
                )
            )

        summaries = await self._summarise(plan_obj, selected[:limit])
        return TaskListing(
            plan=plan_obj,
            tasks=tuple(summaries),
            total=len(selected),
            completed=completed,
            bucket=bucket_obj,
            assignees=tuple(people),
        )

    async def complete_task(self, task_text: str, *, plan: Optional[str] = None) -> CompletionResult:
        """Set a task to 100 %. The task is named by title or ID; `plan` narrows the search."""
        plan_obj = await self.resolve_plan(plan)
        found = await self._find_task(plan_obj, task_text)
        task_id = str(found["id"])
        path = f"/planner/tasks/{_seg(task_id)}"
        allowed = {p.id: p for p in await self.plans()}

        for attempt in range(1, ETAG_ATTEMPTS + 1):
            current = await self._graph.get(path)
            owner = allowed.get(str(current.get("planId")))
            if owner is None:
                # The app's Graph permission can be tenant-wide; the bot stays inside its group.
                raise UserError("That task isn't in one of this group's plans.")  # _find_task rules this out
            if int(current.get("percentComplete") or 0) >= 100:
                summary = (await self._summarise(owner, [current]))[0]
                return CompletionResult(task=current, plan=owner, summary=summary, changed=False)
            try:
                updated = await self._graph.patch(
                    path, json={"percentComplete": 100}, etag=current["@odata.etag"], return_representation=True
                )
            except GraphPreconditionFailed:
                # Someone edited the task in between. Read it again and retry with the new ETag.
                if attempt == ETAG_ATTEMPTS:
                    raise UserError(
                        "That task keeps changing in Planner right now. Please try again in a moment."
                    ) from None
                await self._sleep(0.3 * attempt)
                continue
            if not isinstance(updated, dict):  # Graph may answer 204 despite the Prefer header
                updated = await self._graph.get(path)
            self._remember_task(updated)
            log.info("Completed task %s in plan %s (%s)", task_id, owner.id, owner.title)
            summary = (await self._summarise(owner, [updated]))[0]
            return CompletionResult(task=updated, plan=owner, summary=summary, changed=True)
        raise AssertionError("unreachable")

    async def _find_task(self, plan: Plan, text: str) -> dict[str, Any]:
        wanted = text.strip()
        if not wanted:
            raise UserError("Tell me which task: start typing its title and pick it from the suggestions.")
        tasks: list[dict[str, Any]] = []
        for max_age in _LOOKUP_PASSES:  # second pass: the task may be newer than the cached list
            tasks = await self.tasks(plan.id, max_age=max_age)
            for task in tasks:
                if task.get("id") == wanted:
                    return task
            named = [t for t in tasks if name_key(str(t.get("title") or "")) == name_key(wanted)]
            still_open = [t for t in named if int(t.get("percentComplete") or 0) < 100]
            candidates = still_open or named
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise UserError(
                    f"Plan “{plan.title}” has {len(candidates)} tasks called “{wanted}”. "
                    "Pick the right one from the suggestions."
                )
        if PLANNER_ID_RE.match(wanted):
            # Possibly a task of another plan in the group, picked before the plan option changed.
            # A task anywhere else is treated exactly like one that doesn't exist.
            try:
                elsewhere = await self._graph.get(f"/planner/tasks/{_seg(wanted)}")
            except (GraphNotFound, GraphBadRequest, GraphForbidden):
                elsewhere = None
            if elsewhere is not None and elsewhere.get("planId") in {p.id for p in await self.plans()}:
                return elsewhere
        open_titles = [str(t.get("title") or "") for t in tasks if int(t.get("percentComplete") or 0) < 100]
        raise UserError(
            f"I couldn't find a task called “{wanted}” in plan “{plan.title}”.{did_you_mean(wanted, open_titles)}"
        )

    async def _summarise(self, plan: Plan, tasks: Sequence[dict[str, Any]]) -> list[TaskSummary]:
        """Readable summaries (names instead of IDs). Lookups that fail leave gaps, not errors."""
        bucket_names: dict[str, str] = {}
        label_names: dict[str, str] = {}
        member_names: dict[str, str] = {}
        if tasks:
            try:
                bucket_names = {b.id: b.name for b in await self.buckets(plan.id)}
                label_names = (await self.labels(plan.id)).names
                member_names = {m.id: m.label for m in await self.members()}
            except GraphError as exc:
                log.warning("Could not load names for the task summary: %s", exc)
        summaries = []
        for task in tasks:
            applied = task.get("appliedCategories") or {}
            task_id = str(task.get("id"))
            summaries.append(
                TaskSummary(
                    id=task_id,
                    title=str(task.get("title") or "Untitled task"),
                    bucket=bucket_names.get(str(task.get("bucketId") or "")),
                    labels=tuple(
                        label_names[slot] for slot in LABEL_SLOTS if applied.get(slot) and slot in label_names
                    ),
                    assignees=tuple(member_names.get(user_id, "Unknown") for user_id in assignee_ids(task)),
                    due=parse_graph_datetime(task.get("dueDateTime")),
                    completed_at=parse_graph_datetime(task.get("completedDateTime")),
                    percent_complete=int(task.get("percentComplete") or 0),
                    url=self.task_url(str(task.get("planId") or plan.id), task_id),
                )
            )
        return summaries

    def _remember_task(self, task: dict[str, Any]) -> None:
        """Put a task the bot just created or changed into the cached task list of its plan."""
        plan_id = task.get("planId")
        if not isinstance(plan_id, str):
            return
        cached = self._tasks.peek(plan_id)
        if cached is None:
            return
        others = [t for t in cached if t.get("id") != task.get("id")]
        self._tasks.amend(plan_id, [*others, task])

    # ------------------------------------------------------------------ labels

    async def create_label(self, name: str, *, plan: Optional[str] = None) -> LabelCreated:
        """Give a name to an unused label slot of a plan."""
        name = " ".join(name.split())
        if not name:
            raise UserError("The label needs a name.")
        plan_obj = await self.resolve_plan(plan)
        path = f"/planner/plans/{_seg(plan_obj.id)}/details"

        for attempt in range(1, ETAG_ATTEMPTS + 1):
            details = await self._graph.get(path)
            descriptions = details.get("categoryDescriptions") or {}
            named = PlanLabels.from_details(details)
            existing = named.slot_for(name)
            if existing is not None:
                raise UserError(f"Plan “{plan_obj.title}” already has a label called “{named.names[existing]}”.")
            free = [slot for slot in LABEL_SLOTS if not str(descriptions.get(slot) or "").strip()]
            if not free:
                raise UserError(
                    f"All {len(LABEL_SLOTS)} labels of plan “{plan_obj.title}” already have names. "
                    "Rename one in Planner to reuse it."
                )
            # An unnamed label can still be on tasks (people apply labels by colour).
            # Prefer a slot nobody uses, so naming it doesn't relabel existing tasks.
            usage = await self._label_usage(plan_obj.id)
            slot = next((candidate for candidate in free if not usage.get(candidate)), free[0])
            try:
                await self._graph.patch(path, json={"categoryDescriptions": {slot: name}}, etag=details["@odata.etag"])
            except GraphPreconditionFailed:
                # The plan details changed meanwhile (maybe that very slot was just named). Start over.
                if attempt == ETAG_ATTEMPTS:
                    raise UserError(
                        "The plan's labels keep changing in Planner right now. Please try again in a moment."
                    ) from None
                await self._sleep(0.3 * attempt)
                continue
            self._labels.invalidate(plan_obj.id)
            log.info("Named label slot %s of plan %s: %s", slot, plan_obj.id, name)
            return LabelCreated(plan=plan_obj, slot=slot, name=name, tasks_already_using_slot=usage.get(slot, 0))
        raise AssertionError("unreachable")

    async def _label_usage(self, plan_id: str) -> Counter[str]:
        usage: Counter[str] = Counter()
        try:
            tasks = await self.tasks(plan_id)
        except GraphError as exc:
            log.warning("Could not check which labels are in use: %s", exc)
            return usage
        for task in tasks:
            applied = task.get("appliedCategories") or {}
            usage.update(slot for slot in LABEL_SLOTS if applied.get(slot))
        return usage

    # ------------------------------------------------------------------ for the notifier

    async def bucket_name(self, plan_id: str, bucket_id: Optional[str]) -> Optional[str]:
        if not bucket_id:
            return None
        for max_age in _LOOKUP_PASSES:
            try:
                buckets = await self.buckets(plan_id, max_age=max_age)
            except GraphError:
                return None
            for bucket in buckets:
                if bucket.id == bucket_id:
                    return bucket.name
        return None

    async def user_name(self, user_id: Optional[str]) -> Optional[str]:
        """Display name for a user ID, or None if it can't be found out."""
        if not user_id:
            return None
        try:
            for member in await self.members():
                if member.id == user_id:
                    return member.label
        except GraphError:
            pass
        cached = self._user_names.get(user_id)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        name: Optional[str] = None
        try:
            # Someone outside the group (or the app itself). This lookup needs a broader
            # permission than the member list in app-only mode, so failing is expected.
            user = await self._graph.get(
                f"/users/{_seg(user_id)}", params={"$select": "id,displayName,mail,userPrincipalName"}
            )
            name = str(user.get("displayName") or user.get("mail") or user.get("userPrincipalName") or "") or None
        except GraphError as exc:
            log.debug("Could not look up user %s: %s", user_id, exc)
        if len(self._user_names) > 500:
            self._user_names.clear()
        self._user_names[user_id] = (name, time.monotonic() + (3600.0 if name else 600.0))
        return name
