"""Small async client for Microsoft Graph.

It takes care of the things every Graph caller has to get right:

* bearer tokens, with one refresh-and-retry when Graph answers 401 (expired or revoked token)
* throttling: 429 responses are retried after the `Retry-After` delay, and while a throttle
  is in force every other request waits too instead of adding to it
* transient failures (503, and 502/504/network errors for requests that are safe to repeat)
* paging through `@odata.nextLink`
* typed exceptions, so callers can tell an ETag conflict (412) from a missing item (404)
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import urlsplit

import aiohttp
from yarl import URL

log = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
USER_AGENT = "planner-discord-bot/1.0"


class GraphError(Exception):
    """Microsoft Graph refused or failed a request."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        request_id: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after

    def __str__(self) -> str:
        details = [f"HTTP {self.status}" if self.status else None, self.code, self.message]
        text = " - ".join(str(d) for d in details if d)
        return f"{text} (request-id {self.request_id})" if self.request_id else text


class GraphBadRequest(GraphError):
    """400: Graph (or Planner) rejected the request as invalid."""


class GraphAuthError(GraphError):
    """Authentication failed: no token could be obtained, or Graph answered 401 to a new one."""


class GraphForbidden(GraphError):
    """403: missing permission, no access to the item, or a Planner limit was reached."""


class GraphNotFound(GraphError):
    """404: the item does not exist (or is not visible to this identity)."""


class GraphPreconditionFailed(GraphError):
    """409/412: the ETag sent in If-Match is no longer current. Re-read the item and retry."""


class GraphThrottled(GraphError):
    """429 for longer than the caller is prepared to wait. `retry_after` says how long."""


class GraphUnavailable(GraphError):
    """Graph could not be reached or kept failing with a server error."""


_ERROR_TYPES: dict[int, type[GraphError]] = {
    400: GraphBadRequest,
    401: GraphAuthError,
    403: GraphForbidden,
    404: GraphNotFound,
    409: GraphPreconditionFailed,
    412: GraphPreconditionFailed,
    429: GraphThrottled,
}


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Seconds to wait according to a Retry-After header (delta-seconds or an HTTP date)."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return max(seconds, 0.0)


class GraphClient:
    """Authenticated JSON requests against Microsoft Graph with retries and paging."""

    def __init__(
        self,
        token_provider: Any,
        *,
        base_url: str = GRAPH_BASE_URL,
        session: Any = None,
        timeout: float = 30.0,
        max_attempts: int = 5,
        default_wait: float = 20.0,
        concurrency: int = 4,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tokens = token_provider
        self._base_url = base_url.rstrip("/")
        self._base = urlsplit(self._base_url)
        self._session = session
        self._owns_session = session is None
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._default_wait = default_wait
        self._semaphore = asyncio.Semaphore(concurrency)
        self._sleep = sleep
        self._clock = clock
        self._throttled_until = 0.0

    async def start(self) -> None:
        if self._session is None:
            # trust_env makes aiohttp honour HTTPS_PROXY / NO_PROXY, like MSAL's requests does.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                trust_env=True,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ public API

    async def get(
        self, path: str, *, params: Optional[Mapping[str, str]] = None, max_wait: Optional[float] = None
    ) -> Any:
        return await self.request("GET", path, params=params, max_wait=max_wait)

    async def post(self, path: str, *, json: Any, max_wait: Optional[float] = None) -> Any:
        return await self.request("POST", path, json=json, max_wait=max_wait)

    async def patch(
        self,
        path: str,
        *,
        json: Any,
        etag: str,
        return_representation: bool = False,
        max_wait: Optional[float] = None,
    ) -> Any:
        """PATCH with the If-Match header Planner requires on every update."""
        return await self.request(
            "PATCH", path, json=json, etag=etag, return_representation=return_representation, max_wait=max_wait
        )

    async def get_all(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        max_wait: Optional[float] = None,
        max_pages: int = 200,
    ) -> list[dict[str, Any]]:
        """Collect every item of a collection, following @odata.nextLink."""
        items: list[dict[str, Any]] = []
        target = path
        for _ in range(max_pages):
            page = await self.request("GET", target, params=params, max_wait=max_wait)
            params = None  # a nextLink already carries its query string
            if not isinstance(page, dict):
                raise GraphError(f"Expected a collection from {path}, got {type(page).__name__}")
            items.extend(page.get("value") or [])
            next_link = page.get("@odata.nextLink")
            if not next_link:
                return items
            target = str(next_link)
        raise GraphError(f"Gave up after {max_pages} pages of {path}")

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        json: Any = None,
        etag: Optional[str] = None,
        return_representation: bool = False,
        max_wait: Optional[float] = None,
    ) -> Any:
        """Send one request, retrying what is safe to retry.

        `max_wait` caps the total time spent sleeping between attempts. When Graph asks for
        a longer pause than that, GraphThrottled is raised so interactive callers can tell
        the user instead of hanging.
        """
        if self._session is None:
            raise RuntimeError("GraphClient.start() has not been called")

        method = method.upper()
        url = self._resolve(path)
        headers: dict[str, str] = {}
        if etag is not None:
            headers["If-Match"] = etag
        if return_representation:
            headers["Prefer"] = "return=representation"

        # Reads can always be repeated. So can ETag-guarded writes: if the first attempt did
        # go through, the repeat fails with 412 rather than applying the change twice.
        repeatable = method == "GET" or etag is not None
        budget = self._default_wait if max_wait is None else max_wait
        waited = 0.0
        force_new_token = False
        token_refreshed = False
        attempt = 0

        while True:
            attempt += 1
            waited += await self._wait_for_throttle(budget - waited)
            token = await self._tokens.get_token(force_refresh=force_new_token)
            force_new_token = False

            try:
                status, response_headers, body = await self._send(method, url, token, headers, params, json)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                delay = self._backoff(attempt)
                if not repeatable or attempt >= self._max_attempts or waited + delay > budget:
                    raise GraphUnavailable(
                        f"Could not reach Microsoft Graph ({exc.__class__.__name__}: {exc})"
                    ) from exc
                log.warning("%s %s failed (%s); retrying in %.1fs", method, path, exc.__class__.__name__, delay)
                await self._sleep(delay)
                waited += delay
                continue

            if 200 <= status < 300:
                return body

            request_id = response_headers.get("request-id")

            if status == 401 and not token_refreshed:
                log.info("Graph answered 401 for %s %s; requesting a new token and retrying once", method, path)
                token_refreshed = True
                force_new_token = True
                continue

            if status in (429, 502, 503, 504):
                requested = parse_retry_after(response_headers.get("Retry-After"))
                delay = max(requested, 0.5) if requested is not None else self._backoff(attempt)
                if status == 429:
                    self._throttled_until = max(self._throttled_until, self._clock() + delay)
                # 429 and 503 mean the request was not processed; 502/504 leave that unknown,
                # so only repeatable requests are retried after those.
                may_retry = status in (429, 503) or repeatable
                if may_retry and attempt < self._max_attempts and waited + delay <= budget:
                    log.warning(
                        "Graph answered %s for %s %s; retrying in %.1fs (attempt %d of %d)",
                        status,
                        method,
                        path,
                        delay,
                        attempt,
                        self._max_attempts,
                    )
                    await self._sleep(delay)
                    waited += delay
                    continue
                if status == 429:
                    raise GraphThrottled(
                        "Microsoft Graph is rate limiting this app",
                        status=status,
                        request_id=request_id,
                        retry_after=delay,
                    )

            raise _error_from_response(status, body, request_id)

    # ------------------------------------------------------------------ internals

    def _resolve(self, path: str) -> str:
        """Turn a path into a full URL, refusing links that point away from Graph."""
        if path.startswith(("http://", "https://")):
            target = urlsplit(path)
            if (target.scheme, target.netloc.lower()) != (self._base.scheme, self._base.netloc.lower()):
                # The bearer token must never be sent to another host.
                raise GraphError(f"Refusing to follow a link outside Microsoft Graph: {target.netloc}")
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _backoff(self, attempt: int) -> float:
        return min(2.0 ** (attempt - 1), 30.0) + random.uniform(0.0, 0.5)

    async def _wait_for_throttle(self, remaining: float) -> float:
        """Honour a throttle another request ran into. Returns the time spent waiting."""
        delay = self._throttled_until - self._clock()
        if delay <= 0:
            return 0.0
        if delay > remaining:
            raise GraphThrottled("Microsoft Graph is rate limiting this app", status=429, retry_after=delay)
        await self._sleep(delay)
        return delay

    async def _send(
        self,
        method: str,
        url: str,
        token: str,
        headers: Mapping[str, str],
        params: Optional[Mapping[str, str]],
        json: Any,
    ) -> tuple[int, Mapping[str, str], Any]:
        request_headers = {"Authorization": f"Bearer {token}", **headers}
        # A nextLink is sent exactly as Graph wrote it; re-encoding can corrupt its skip token.
        target: Any = URL(url, encoded=True) if params is None else url
        async with self._semaphore:
            started = self._clock()
            async with self._session.request(
                method, target, params=params, json=json, headers=request_headers
            ) as response:
                status = response.status
                response_headers = response.headers
                text = await response.text(errors="replace")
        log.debug("%s %s -> %s in %.0f ms", method, url, status, (self._clock() - started) * 1000)

        body: Any = None
        if text:
            try:
                body = jsonlib.loads(text)
            except ValueError:
                body = text
        return status, response_headers, body


def _error_from_response(status: int, body: Any, request_id: Optional[str]) -> GraphError:
    code: Optional[str] = None
    message: Optional[str] = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code") or None
            message = error.get("message") or None
            inner = error.get("innerError") or error.get("innererror") or {}
            if isinstance(inner, dict):
                request_id = request_id or inner.get("request-id")
    elif isinstance(body, str):
        message = body.strip()[:300] or None

    error_type = _ERROR_TYPES.get(status)
    if error_type is None:
        error_type = GraphUnavailable if status >= 500 else GraphError
    return error_type(
        message or f"Microsoft Graph answered HTTP {status}", status=status, code=code, request_id=request_id
    )
