"""An in-memory stand-in for the parts of Microsoft Graph the bot uses.

It behaves like Planner where that matters to the bot: every object carries an
``@odata.etag`` that changes on each update, PATCH without the current ETag in ``If-Match``
answers 412, completing a task stamps ``completedDateTime`` and ``completedBy``, task
listings can be paged, and requests need a valid bearer token. Tests can also queue
failures (429 with Retry-After, 503, network errors, ...) for upcoming requests.

``FakeGraph().session`` is passed to ``GraphClient`` in place of an aiohttp session.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

BASE_URL = "https://graph.test/v1.0"
GROUP_ID = "11111111-aaaa-4bbb-8ccc-222222222222"


@dataclass
class Recorded:
    method: str
    path: str
    params: dict[str, str]  # the query as Graph sees it: from the URL plus `sent_params`
    json: Any
    headers: dict[str, str]
    sent_params: dict[str, str]  # only what the client passed separately from the URL


@dataclass
class _Fault:
    status: Optional[int]
    method: Optional[str]
    path: Optional[re.Pattern[str]]
    headers: dict[str, str]
    body: Any
    exception: Optional[BaseException]
    times: int


@dataclass
class _Hook:
    method: str
    path: re.Pattern[str]
    action: Callable[[], None]


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: Any = None,
        headers: Optional[dict[str, str]] = None,
        exception: Optional[BaseException] = None,
    ) -> None:
        self.status = status
        self.headers = {"request-id": "req-test", **(headers or {})}
        self._body = body
        self._exception = exception

    async def __aenter__(self) -> "FakeResponse":
        if self._exception is not None:
            raise self._exception
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def text(self, encoding: Optional[str] = None, errors: str = "strict") -> str:
        if self._body is None:
            return ""
        return self._body if isinstance(self._body, str) else json.dumps(self._body)


class FakeSession:
    """The slice of aiohttp.ClientSession that GraphClient uses."""

    def __init__(self, graph: "FakeGraph") -> None:
        self._graph = graph
        self.closed = False

    def request(
        self, method: str, url: Any, *, params: Any = None, json: Any = None, headers: Any = None
    ) -> FakeResponse:
        return self._graph.handle(method, str(url), dict(params or {}), json, dict(headers or {}))

    async def close(self) -> None:
        self.closed = True


def _error(status: int, code: str, message: str) -> FakeResponse:
    return FakeResponse(status, {"error": {"code": code, "message": message, "innerError": {"request-id": "req-test"}}})


@dataclass
class FakeGraph:
    group_id: str = GROUP_ID
    valid_tokens: set[str] = field(default_factory=set)
    plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    buckets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    plan_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    members: list[dict[str, Any]] = field(default_factory=list)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)
    page_size: Optional[int] = None  # page task listings like Graph does
    details_lag: int = 0  # how often a new task's details answer 404 first
    honour_prefer: bool = True  # False: answer 204 even to "Prefer: return=representation"
    actor: str = "00000000-0000-4000-8000-0000000b0700"  # who "the bot" is to Planner
    now: datetime = datetime(2026, 10, 5, 16, 0, 0, tzinfo=timezone.utc)

    def __post_init__(self) -> None:
        self._serial = itertools.count(1)
        self._faults: list[_Fault] = []
        self._hooks: list[_Hook] = []
        self._details_misses: dict[str, int] = {}
        self.session = FakeSession(self)

    # ------------------------------------------------------------------ building the world

    def _etag(self) -> str:
        return f'W/"etag-{next(self._serial):06d}"'

    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._serial):0{28 - len(prefix)}d}"

    def tick(self, seconds: float = 60.0) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now

    def stamp(self, moment: Optional[datetime] = None) -> str:
        """A timestamp the way Graph writes them: seven decimals and a trailing Z."""
        moment = moment or self.now
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond:06d}0Z"

    def add_plan(
        self,
        title: str,
        *,
        plan_id: Optional[str] = None,
        labels: Optional[dict[str, str]] = None,
        container_type: str = "group",
        group_id: Optional[str] = None,
    ) -> str:
        """Add a plan. Pass another `group_id` for a plan that belongs to a different group."""
        plan_id = plan_id or self._id("plan")
        owner = group_id or self.group_id
        self.plans[plan_id] = {
            "id": plan_id,
            "title": title,
            "owner": owner,
            "container": {"containerId": owner, "type": container_type, "url": "https://graph.test/groups/x"},
            "@odata.etag": self._etag(),
        }
        descriptions: dict[str, Optional[str]] = {f"category{n}": None for n in range(1, 26)}
        descriptions.update(labels or {})
        self.plan_details[plan_id] = {"id": plan_id, "@odata.etag": self._etag(), "categoryDescriptions": descriptions}
        self.buckets[plan_id] = []
        return plan_id

    def add_bucket(self, plan_id: str, name: str, *, order_hint: Optional[str] = None) -> str:
        bucket_id = self._id("bucket")
        hint = order_hint if order_hint is not None else f"{len(self.buckets[plan_id]):04d}"
        self.buckets[plan_id].append(
            {"id": bucket_id, "name": name, "planId": plan_id, "orderHint": hint, "@odata.etag": self._etag()}
        )
        return bucket_id

    def add_member(self, name: str, email: str, *, user_id: Optional[str] = None, upn: Optional[str] = None) -> str:
        user_id = user_id or f"00000000-0000-4000-8000-{next(self._serial):012d}"
        user = {
            "@odata.type": "#microsoft.graph.user",
            "id": user_id,
            "displayName": name,
            "mail": email,
            "userPrincipalName": upn or email,
        }
        self.members.append(user)
        self.users[user_id] = user
        return user_id

    def add_task(
        self,
        plan_id: str,
        title: str,
        *,
        bucket_id: Optional[str] = None,
        percent: int = 0,
        assignees: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
        due: Optional[str] = None,
        completed_at: Optional[datetime] = None,
        completed_by: Optional[str] = None,
    ) -> str:
        task_id = self._id("task")
        task = {
            "id": task_id,
            "planId": plan_id,
            "bucketId": bucket_id,
            "title": title,
            "percentComplete": 0,
            "dueDateTime": due,
            "completedDateTime": None,
            "completedBy": None,
            "assignments": {
                a: {"@odata.type": "#microsoft.graph.plannerAssignment", "orderHint": "x"} for a in assignees
            },
            "appliedCategories": {slot: True for slot in labels},
            "createdDateTime": self.stamp(),
            "@odata.etag": self._etag(),
        }
        self.tasks[task_id] = task
        self.task_details[task_id] = {"id": task_id, "description": "", "@odata.etag": self._etag()}
        if percent:
            self.set_progress(task_id, percent, by=completed_by, at=completed_at)
        return task_id

    # ------------------------------------------------------------------ things other people do in Planner

    def set_progress(
        self, task_id: str, percent: int, *, by: Optional[str] = None, at: Optional[datetime] = None
    ) -> None:
        task = self.tasks[task_id]
        was_complete = task["percentComplete"] >= 100
        task["percentComplete"] = percent
        if percent >= 100 and not was_complete:
            task["completedDateTime"] = self.stamp(at)
            task["completedBy"] = {"user": {"displayName": None, "id": by or self.actor}}
        elif percent < 100:
            task["completedDateTime"] = None
            task["completedBy"] = None
        task["@odata.etag"] = self._etag()

    def complete(self, task_id: str, *, by: Optional[str] = None, at: Optional[datetime] = None) -> None:
        self.set_progress(task_id, 100, by=by, at=at)

    def reopen(self, task_id: str) -> None:
        self.set_progress(task_id, 0)

    def touch(self, task_id: str) -> None:
        """Somebody edits the task: its ETag changes."""
        self.tasks[task_id]["@odata.etag"] = self._etag()

    def delete_task(self, task_id: str) -> None:
        del self.tasks[task_id]
        self.task_details.pop(task_id, None)

    # ------------------------------------------------------------------ fault injection

    def fail_next(
        self,
        status: Optional[int] = None,
        *,
        method: Optional[str] = None,
        path: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        body: Any = None,
        exception: Optional[BaseException] = None,
        times: int = 1,
    ) -> None:
        """Make the next matching request(s) fail with `status` (or raise `exception`)."""
        self._faults.append(
            _Fault(status, method, re.compile(path) if path else None, headers or {}, body, exception, times)
        )

    def before_next(self, method: str, path: str, action: Callable[[], None]) -> None:
        """Run `action` just before the next matching request is processed (a concurrent edit, say)."""
        self._hooks.append(_Hook(method, re.compile(path), action))

    def calls(self, method: Optional[str] = None, path: Optional[str] = None) -> list[Recorded]:
        pattern = re.compile(path) if path else None
        return [
            r
            for r in self.requests
            if (method is None or r.method == method) and (pattern is None or pattern.search(r.path))
        ]

    # ------------------------------------------------------------------ request handling

    def handle(self, method: str, url: str, params: dict[str, str], body: Any, headers: dict[str, str]) -> FakeResponse:
        split = urlsplit(url)
        assert url.startswith(BASE_URL), f"request left the fake Graph: {url}"
        path = split.path[len(urlsplit(BASE_URL).path) :]
        query = {key: values[0] for key, values in parse_qs(split.query).items()}
        query.update(params)
        self.requests.append(Recorded(method, path, query, body, headers, dict(params)))

        for fault in self._faults:
            if fault.times > 0 and (fault.method in (None, method)) and (fault.path is None or fault.path.search(path)):
                fault.times -= 1
                if fault.exception is not None:
                    return FakeResponse(0, exception=fault.exception)
                payload = fault.body
                if payload is None:
                    payload = {"error": {"code": "injected", "message": f"injected {fault.status}"}}
                return FakeResponse(fault.status or 500, payload, fault.headers)

        token = headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid_tokens:
            return _error(401, "InvalidAuthenticationToken", "Access token has expired or is not yet valid.")

        for hook in list(self._hooks):
            if hook.method == method and hook.path.search(path):
                self._hooks.remove(hook)
                hook.action()

        for pattern, handler in self._routes():
            match = re.fullmatch(pattern, path)
            if match:
                return handler(method, query, body, headers, *match.groups())
        return _error(404, "ResourceNotFound", f"No fake route for {method} {path}")

    def _routes(self) -> list[tuple[str, Callable[..., FakeResponse]]]:
        return [
            (r"/planner/plans/([^/]+)", self._plan),
            (r"/groups/([^/]+)/planner/plans", self._group_plans),
            (r"/planner/plans/([^/]+)/buckets", self._plan_buckets),
            (r"/planner/plans/([^/]+)/details", self._plan_details),
            (r"/planner/plans/([^/]+)/tasks", self._plan_tasks),
            (r"/planner/tasks", self._create_task),
            (r"/planner/tasks/([^/]+)", self._task),
            (r"/planner/tasks/([^/]+)/details", self._task_details),
            (r"/groups/([^/]+)/members", self._group_members),
            (r"/users/([^/]+)", self._user),
        ]

    @staticmethod
    def _precondition(headers: dict[str, str], resource: dict[str, Any]) -> Optional[FakeResponse]:
        supplied = headers.get("If-Match")
        if supplied is None:
            return _error(412, "", "The If-Match header must be specified for this kind of request.")
        if supplied != resource["@odata.etag"]:
            return _error(412, "", "The If-Match header contains an etag that does not match the current resource.")
        return None

    def _after_write(self, headers: dict[str, str], resource: dict[str, Any]) -> FakeResponse:
        if self.honour_prefer and headers.get("Prefer") == "return=representation":
            return FakeResponse(200, dict(resource))
        return FakeResponse(204)

    def _plan(self, method: str, query: dict, body: Any, headers: dict, plan_id: str) -> FakeResponse:
        plan = self.plans.get(plan_id)
        return FakeResponse(200, dict(plan)) if plan else _error(404, "", "The requested item is not found.")

    def _group_plans(self, method: str, query: dict, body: Any, headers: dict, group_id: str) -> FakeResponse:
        if group_id != self.group_id:
            return _error(404, "Request_ResourceNotFound", "Group not found.")
        return FakeResponse(200, {"value": [dict(plan) for plan in self.plans.values() if plan["owner"] == group_id]})

    def _plan_buckets(self, method: str, query: dict, body: Any, headers: dict, plan_id: str) -> FakeResponse:
        if plan_id not in self.plans:
            return _error(404, "", "The requested item is not found.")
        return FakeResponse(200, {"value": [dict(bucket) for bucket in self.buckets[plan_id]]})

    def _plan_details(self, method: str, query: dict, body: Any, headers: dict, plan_id: str) -> FakeResponse:
        details = self.plan_details.get(plan_id)
        if details is None:
            return _error(404, "", "The requested item is not found.")
        if method == "GET":
            return FakeResponse(200, json.loads(json.dumps(details)))
        failed = self._precondition(headers, details)
        if failed:
            return failed
        for slot, name in (body.get("categoryDescriptions") or {}).items():
            if slot not in details["categoryDescriptions"]:
                return _error(400, "", f"The request is invalid: unknown category {slot}.")
            details["categoryDescriptions"][slot] = name
        details["@odata.etag"] = self._etag()
        return self._after_write(headers, details)

    def _plan_tasks(self, method: str, query: dict, body: Any, headers: dict, plan_id: str) -> FakeResponse:
        if plan_id not in self.plans:
            return _error(404, "", "The requested item is not found.")
        tasks = [json.loads(json.dumps(task)) for task in self.tasks.values() if task["planId"] == plan_id]
        if not self.page_size:
            return FakeResponse(200, {"value": tasks})
        start = int(query.get("$skiptoken", 0))
        page = {"value": tasks[start : start + self.page_size]}
        if start + self.page_size < len(tasks):
            page["@odata.nextLink"] = f"{BASE_URL}/planner/plans/{plan_id}/tasks?$skiptoken={start + self.page_size}"
        return FakeResponse(200, page)

    def _create_task(self, method: str, query: dict, body: Any, headers: dict) -> FakeResponse:
        if method != "POST":
            return _error(405, "", "Method not allowed.")
        plan_id = body.get("planId")
        if plan_id not in self.plans:
            return _error(400, "", "The request is invalid: planId does not reference an existing plan.")
        if not str(body.get("title") or "").strip():
            return _error(400, "", "The request is invalid: a title is required.")
        bucket_id = body.get("bucketId")
        if bucket_id is not None and bucket_id not in {b["id"] for b in self.buckets[plan_id]}:
            return _error(400, "", "The request is invalid: the bucket is not part of the plan.")
        for assignment in (body.get("assignments") or {}).values():
            if assignment.get("@odata.type") != "#microsoft.graph.plannerAssignment" or "orderHint" not in assignment:
                return _error(400, "", "The request is invalid: assignments need @odata.type and orderHint.")
        for slot in body.get("appliedCategories") or {}:
            if not re.fullmatch(r"category([1-9]|1\d|2[0-5])", slot):
                return _error(400, "", f"The request is invalid: {slot} is not a category.")
        task_id = self._id("task")
        task = {
            "id": task_id,
            "planId": plan_id,
            "bucketId": bucket_id,
            "title": body["title"],
            "percentComplete": 0,
            "dueDateTime": body.get("dueDateTime"),
            "completedDateTime": None,
            "completedBy": None,
            "assignments": body.get("assignments") or {},
            "appliedCategories": body.get("appliedCategories") or {},
            "createdDateTime": self.stamp(),
            "createdBy": {"user": {"displayName": None, "id": self.actor}},
            "@odata.etag": self._etag(),
        }
        self.tasks[task_id] = task
        self.task_details[task_id] = {"id": task_id, "description": "", "@odata.etag": self._etag()}
        self._details_misses[task_id] = self.details_lag
        return FakeResponse(201, dict(task))

    def _task(self, method: str, query: dict, body: Any, headers: dict, task_id: str) -> FakeResponse:
        task = self.tasks.get(task_id)
        if task is None:
            return _error(404, "", "The requested item is not found.")
        if method == "GET":
            return FakeResponse(200, json.loads(json.dumps(task)))
        failed = self._precondition(headers, task)
        if failed:
            return failed
        if "percentComplete" in body:
            self.set_progress(task_id, int(body["percentComplete"]))
        for key in ("title", "bucketId", "dueDateTime"):
            if key in body:
                task[key] = body[key]
                task["@odata.etag"] = self._etag()
        return self._after_write(headers, task)

    def _task_details(self, method: str, query: dict, body: Any, headers: dict, task_id: str) -> FakeResponse:
        details = self.task_details.get(task_id)
        if details is None:
            return _error(404, "", "The requested item is not found.")
        if method == "GET":
            if self._details_misses.get(task_id, 0) > 0:
                self._details_misses[task_id] -= 1
                return _error(404, "", "The requested item is not found.")
            return FakeResponse(200, dict(details))
        failed = self._precondition(headers, details)
        if failed:
            return failed
        details.update({key: value for key, value in body.items() if key in ("description", "previewType")})
        details["@odata.etag"] = self._etag()
        return self._after_write(headers, details)

    def _group_members(self, method: str, query: dict, body: Any, headers: dict, group_id: str) -> FakeResponse:
        if group_id != self.group_id:
            return _error(404, "Request_ResourceNotFound", "Group not found.")
        return FakeResponse(200, {"value": [dict(member) for member in self.members]})

    def _user(self, method: str, query: dict, body: Any, headers: dict, user_id: str) -> FakeResponse:
        user = self.users.get(user_id)
        return FakeResponse(200, dict(user)) if user else _error(404, "Request_ResourceNotFound", "User not found.")
