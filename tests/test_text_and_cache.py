"""Pure helpers: list parsing, list autocomplete, ranking, dates, and the TTL cache."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from helpers import async_test
from cache import TTLCache
from matching import ListCandidate, name_key, rank_matches, split_names, suggest_list
from planner_service import UserError, due_moment, parse_due_date, parse_graph_datetime, to_graph_datetime

LABELS = [
    ListCandidate("Bug"),
    ListCandidate("Bugfix"),
    ListCandidate("Frontend"),
    ListCandidate("Backend"),
    ListCandidate("Needs review, urgent"),
]
PEOPLE = [
    ListCandidate("Anna Kowalska", ("Anna Kowalska", "anna.kowalska@contoso.com")),
    ListCandidate("Jan Nowak", ("Jan Nowak", "jan.nowak@contoso.com")),
    ListCandidate("Kowalski, Piotr", ("Kowalski, Piotr", "piotr.kowalski@contoso.com")),
]


def test_name_key_ignores_case_and_spacing():
    assert name_key("  Needs   Review ,  URGENT ") == name_key("needs review,urgent")
    assert name_key("Straße") == name_key("STRASSE")


def test_split_names_basic():
    assert split_names("Bug, Frontend") == ["Bug", "Frontend"]
    assert split_names(" Bug ;Frontend,, ") == ["Bug", "Frontend"]
    assert split_names("bug, BUG, Bug") == ["bug"]
    assert split_names("") == []


def test_split_names_keeps_known_names_that_contain_a_comma():
    known = ["Bug", "Needs review, urgent", "Kowalski, Piotr"]
    assert split_names("Bug, needs review, urgent", known) == ["Bug", "needs review, urgent"]
    assert split_names("Kowalski, Piotr; Bug", known) == ["Kowalski, Piotr", "Bug"]
    assert split_names("Kowalski, Piotr, Bug", known) == ["Kowalski, Piotr", "Bug"]
    # Without the vocabulary the same text is four separate names.
    assert split_names("Kowalski, Piotr, Bug") == ["Kowalski", "Piotr", "Bug"]


def test_rank_matches_orders_exact_prefix_word_substring():
    names = ["Backend bug", "Debug", "Bugfix", "Bug"]
    assert rank_matches("bug", names, lambda n: (n,)) == ["Bug", "Bugfix", "Backend bug", "Debug"]
    assert rank_matches("", names, lambda n: (n,)) == names
    assert rank_matches("zzz", names, lambda n: (n,)) == []


def test_suggest_list_completes_the_first_entry():
    assert suggest_list("", LABELS)[:2] == ["Bug", "Bugfix"]
    assert suggest_list("fro", LABELS) == ["Frontend"]
    assert suggest_list("bug", LABELS) == ["Bug", "Bugfix"]


def test_suggest_list_keeps_what_was_already_chosen():
    assert suggest_list("Bug, fro", LABELS) == ["Bug, Frontend"]
    assert suggest_list("bug,back", LABELS) == ["Bug, Backend"]
    # Already chosen entries are not offered again.
    assert suggest_list("Bug, ", LABELS) == [
        "Bug, Bugfix",
        "Bug, Frontend",
        "Bug, Backend",
        "Bug; Needs review, urgent",
    ]
    assert suggest_list("Bug, bug", LABELS) == ["Bug, Bugfix"]


def test_suggest_list_handles_names_with_commas():
    assert suggest_list("needs rev", LABELS) == ["Needs review, urgent"]
    assert suggest_list("Needs review, urg", LABELS) == ["Needs review, urgent"]
    # A list containing such a name is joined with semicolons, so it reads back unambiguously.
    assert suggest_list("Needs review, urgent, fro", LABELS) == ["Needs review, urgent; Frontend"]
    assert split_names("Needs review, urgent; Frontend", [c.token for c in LABELS]) == [
        "Needs review, urgent",
        "Frontend",
    ]


def test_suggest_list_finds_people_by_name_or_email():
    assert suggest_list("kowal", PEOPLE) == ["Kowalski, Piotr", "Anna Kowalska"]
    assert suggest_list("jan.n", PEOPLE) == ["Jan Nowak"]
    assert suggest_list("Kowalski, P", PEOPLE) == ["Kowalski, Piotr"]
    # An email typed for an earlier entry is replaced by the person's name.
    assert suggest_list("anna.kowalska@contoso.com, ja", PEOPLE) == ["Anna Kowalska, Jan Nowak"]


def test_suggest_list_still_helps_after_a_typo_and_respects_the_length_limit():
    assert suggest_list("Bgu, fro", LABELS) == ["Bgu, Frontend"]
    assert suggest_list("Bug, zzz", LABELS) == []
    long_names = [ListCandidate("A" * 60), ListCandidate("B" * 60)]
    assert suggest_list("A" * 60 + ", ", long_names) == []  # 60 + 2 + 60 characters won't fit in a choice
    assert len(suggest_list("", [ListCandidate(f"Label {n}") for n in range(40)])) == 25


def test_parse_graph_datetime():
    parsed = parse_graph_datetime("2026-10-05T16:02:11.1234567Z")
    assert parsed == datetime(2026, 10, 5, 16, 2, 11, 123456, tzinfo=timezone.utc)
    assert parse_graph_datetime("2026-10-05T16:02:11Z") == datetime(2026, 10, 5, 16, 2, 11, tzinfo=timezone.utc)
    assert parse_graph_datetime("2026-10-05T18:02:11.5+02:00") == datetime(
        2026, 10, 5, 16, 2, 11, 500000, tzinfo=timezone.utc
    )
    assert parse_graph_datetime(None) is None
    assert parse_graph_datetime("not a date") is None


def test_due_dates():
    assert parse_due_date(None) is None
    assert parse_due_date("  ") is None
    assert parse_due_date("2026-10-19") == date(2026, 10, 19)
    assert to_graph_datetime(due_moment(date(2026, 10, 19))) == "2026-10-19T10:00:00Z"
    for wrong in ("19.10.2026", "2026-13-01", "2026-02-30", "20261019", "tomorrow", "2026-1-9"):
        with pytest.raises(UserError, match="YYYY-MM-DD"):
            parse_due_date(wrong)


class Loader:
    def __init__(self, delay: float = 0.0):
        self.calls = 0
        self.delay = delay
        self.fail = False

    async def __call__(self) -> int:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return self.calls


@async_test
async def test_cache_serves_fresh_values_and_reloads_after_the_ttl():
    now = [0.0]
    cache: TTLCache[str, int] = TTLCache(60, name="t", clock=lambda: now[0])
    loader = Loader()
    assert await cache.get("k", loader) == 1
    assert await cache.get("k", loader) == 1
    now[0] = 61
    assert await cache.get("k", loader) == 2
    # A caller can ask for something more recent than the TTL guarantees.
    now[0] = 70
    assert await cache.get("k", loader, max_age=5) == 3
    assert await cache.get("k", loader, max_age=5) == 3
    assert await cache.get("k", loader, max_age=0) == 4


@async_test
async def test_amending_a_value_does_not_make_it_look_newer():
    now = [0.0]
    cache: TTLCache[str, int] = TTLCache(60, name="t", clock=lambda: now[0])
    loader = Loader()
    assert await cache.get("k", loader) == 1
    now[0] = 50
    cache.amend("k", 100)  # a local change to data that was loaded 50 seconds ago
    assert await cache.get("k", loader) == 100  # still within the TTL
    assert await cache.get("k", loader, max_age=5) == 2  # but not "recent"
    cache.amend("missing", 1)  # nothing cached: nothing to amend
    assert cache.peek("missing") is None


@async_test
async def test_cache_shares_one_load_between_concurrent_callers():
    cache: TTLCache[str, int] = TTLCache(60, name="t")
    loader = Loader(delay=0.01)
    results = await asyncio.gather(*(cache.get("k", loader) for _ in range(5)))
    assert results == [1] * 5 and loader.calls == 1


@async_test
async def test_get_fast_returns_stale_data_at_once_and_refreshes_behind_the_scenes():
    now = [0.0]
    cache: TTLCache[str, int] = TTLCache(60, name="t", clock=lambda: now[0])
    loader = Loader(delay=0.01)
    assert await cache.get("k", loader) == 1
    now[0] = 500
    assert await cache.get_fast("k", loader, wait=0.001) == 1  # stale, but immediate
    await asyncio.sleep(0.05)
    assert cache.peek("k") == 2 and loader.calls == 2  # refreshed in the background


@async_test
async def test_get_fast_gives_up_waiting_but_lets_the_load_finish():
    cache: TTLCache[str, int] = TTLCache(60, name="t")
    loader = Loader(delay=0.05)
    assert await cache.get_fast("k", loader, wait=0.005) is None
    await asyncio.sleep(0.1)
    assert await cache.get_fast("k", loader, wait=0.005) == 1
    assert loader.calls == 1


@async_test
async def test_cache_failures_reach_get_but_not_get_fast():
    cache: TTLCache[str, int] = TTLCache(60, name="t")
    loader = Loader()
    loader.fail = True
    with pytest.raises(RuntimeError):
        await cache.get("k", loader)
    assert await cache.get_fast("k", loader, wait=1) is None
    loader.fail = False
    assert await cache.get("k", loader) == 3


@async_test
async def test_invalidate_discards_a_load_that_started_earlier():
    cache: TTLCache[str, int] = TTLCache(60, name="t")
    slow = Loader(delay=0.03)
    pending = asyncio.ensure_future(cache.get("k", slow))
    await asyncio.sleep(0.005)
    cache.invalidate("k")  # e.g. the bot just changed the plan's labels
    assert await pending == 1  # the caller still gets its answer
    assert cache.peek("k") is None  # but the pre-change value was not cached
    assert await cache.get("k", slow) == 2
