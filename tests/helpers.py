"""Shared pieces for the tests: an async test decorator, fake time, fake tokens, and a ready-made world."""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from fake_graph import BASE_URL, FakeGraph
from graph_client import GraphClient
from planner_service import PlannerService

TENANT_ID = "99999999-8888-4777-8666-555555555555"


def async_test(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run an `async def` test to completion (no pytest plugin needed)."""

    @functools.wraps(test)
    def runner(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return runner


@dataclass
class FakeTime:
    """A clock that only moves when something sleeps, so retries don't slow the tests down."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeTokens:
    """Hands out token-1, token-2, ... and registers each one as valid with the fake Graph."""

    def __init__(self, graph: FakeGraph) -> None:
        self._graph = graph
        self.current: str | None = None
        self.issued = 0
        self.forced = 0

    async def get_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self.forced += 1
        if force_refresh or self.current is None:
            self.issued += 1
            self.current = f"token-{self.issued}"
            self._graph.valid_tokens.add(self.current)
        return self.current


@dataclass
class World:
    graph: FakeGraph
    time: FakeTime
    tokens: FakeTokens
    client: GraphClient
    planner: PlannerService
    plan_id: str
    todo: str  # bucket IDs of the default plan
    doing: str
    anna: str  # member IDs
    jan: str


def make_world(*, default_bucket: str | None = None, cache_ttl: float = 300.0, **client_options: Any) -> World:
    """A group with two plans, a few buckets, labels and members, plus a client and service wired to it."""
    graph = FakeGraph()
    plan_id = graph.add_plan(
        "Sprint Board", labels={"category1": "Bug", "category3": "Frontend", "category5": "Needs review, urgent"}
    )
    # Created out of order on purpose: the board order comes from the order hints.
    doing = graph.add_bucket(plan_id, "In progress", order_hint="2000")
    graph.add_bucket(plan_id, "Done", order_hint="3000")
    todo = graph.add_bucket(plan_id, "To do", order_hint="1000")
    other = graph.add_plan("Marketing", labels={"category2": "Campaign"})
    graph.add_bucket(other, "Ideas")
    anna = graph.add_member("Anna Kowalska", "anna.kowalska@contoso.com")
    jan = graph.add_member("Jan Nowak", "jan.nowak@contoso.com")
    graph.add_member("Kowalski, Piotr", "piotr.kowalski@contoso.com")
    graph.members.append({"@odata.type": "#microsoft.graph.servicePrincipal", "id": "sp-1", "displayName": "Some app"})

    time = FakeTime()
    tokens = FakeTokens(graph)
    client = GraphClient(
        tokens, base_url=BASE_URL, session=graph.session, sleep=time.sleep, clock=time.clock, **client_options
    )
    planner = PlannerService(
        client,
        default_plan_id=plan_id,
        tenant_id=TENANT_ID,
        default_bucket=default_bucket,
        cache_ttl=cache_ttl,
        sleep=time.sleep,
    )
    return World(graph, time, tokens, client, planner, plan_id, todo, doing, anna, jan)


def restarted(world: World) -> World:
    """The same Planner data seen by a freshly started process: new client, new service, empty caches."""
    time = FakeTime()
    tokens = FakeTokens(world.graph)
    client = GraphClient(tokens, base_url=BASE_URL, session=world.graph.session, sleep=time.sleep, clock=time.clock)
    planner = PlannerService(client, default_plan_id=world.plan_id, tenant_id=TENANT_ID, sleep=time.sleep)
    return World(
        world.graph, time, tokens, client, planner, world.plan_id, world.todo, world.doing, world.anna, world.jan
    )
