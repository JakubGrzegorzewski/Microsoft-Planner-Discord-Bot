"""A small async cache with a time-to-live, built for Discord's three-second autocomplete limit."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Awaitable, Callable, Generic, Hashable, Optional, TypeVar

log = logging.getLogger(__name__)

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Async cache with a time-to-live.

    * ``get``      returns a value no older than the TTL (or than ``max_age``, when given),
                   loading it when needed. Concurrent callers share one load.
    * ``get_fast`` returns what is cached right now, even if stale (a refresh then runs in
                   the background), and waits at most ``wait`` seconds when nothing is
                   cached yet. This is what autocomplete uses.
    """

    def __init__(self, ttl: float, *, name: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._name = name
        self._clock = clock
        self._values: dict[K, tuple[V, float]] = {}
        self._loading: dict[K, asyncio.Future[V]] = {}
        self._generation: dict[K, int] = {}

    def peek(self, key: K) -> Optional[V]:
        entry = self._values.get(key)
        return entry[0] if entry is not None else None

    def is_fresh(self, key: K, max_age: Optional[float] = None) -> bool:
        entry = self._values.get(key)
        limit = self._ttl if max_age is None else max_age
        return entry is not None and self._clock() - entry[1] < limit

    def amend(self, key: K, value: V) -> None:
        """Replace a cached value without making it look newer than it is."""
        entry = self._values.get(key)
        if entry is not None:
            self._values[key] = (value, entry[1])

    def invalidate(self, key: K) -> None:
        """Forget a value. A load already under way will not be allowed to bring it back."""
        self._values.pop(key, None)
        self._loading.pop(key, None)
        self._generation[key] = self._generation.get(key, 0) + 1

    async def get(self, key: K, loader: Callable[[], Awaitable[V]], *, max_age: Optional[float] = None) -> V:
        if self.is_fresh(key, max_age):
            return self._values[key][0]
        # shield: a caller that gives up (or is cancelled) must not abort the shared load.
        return await asyncio.shield(self._start(key, loader))

    async def get_fast(self, key: K, loader: Callable[[], Awaitable[V]], *, wait: float) -> Optional[V]:
        entry = self._values.get(key)
        if entry is not None:
            if not self.is_fresh(key):
                self._start(key, loader)
            return entry[0]
        try:
            return await asyncio.wait_for(asyncio.shield(self._start(key, loader)), wait)
        except asyncio.TimeoutError:
            return None
        except Exception:  # the load failed; _finished() has logged it
            return None

    def _start(self, key: K, loader: Callable[[], Awaitable[V]]) -> "asyncio.Future[V]":
        running = self._loading.get(key)
        if running is None:
            running = asyncio.ensure_future(self._load(key, loader, self._generation.get(key, 0)))
            self._loading[key] = running
            running.add_done_callback(functools.partial(self._finished, key))
        return running

    async def _load(self, key: K, loader: Callable[[], Awaitable[V]], generation: int) -> V:
        value = await loader()
        if self._generation.get(key, 0) == generation:
            self._values[key] = (value, self._clock())
        return value

    def _finished(self, key: K, done: "asyncio.Future[V]") -> None:
        if self._loading.get(key) is done:
            del self._loading[key]
        # Reading the exception marks it as handled, so background refreshes that fail
        # don't produce "exception was never retrieved" noise.
        if not done.cancelled() and done.exception() is not None:
            log.debug("Loading %s[%r] failed: %s", self._name, key, done.exception())
