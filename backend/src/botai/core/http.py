"""Outbound HTTP to universities: timeouts, bounded retries, host allowlist (docs/architecture/lld.md §5).

Every request to an external site goes through `request_with_retries` on a client from
`build_client`. The real enforcement of the allowlist is the EgressProxy; the check here is a
second line of defence and gives a clear `egress_blocked` error early.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from botai.core.errors import BotaiError, ErrorCode

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
TOTAL_BUDGET_S = 30.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 4  # 1 + 3 retries
    backoff_s: tuple[float, ...] = (0.5, 2.0, 8.0)
    max_retry_after_s: float = 60.0
    jitter: float = 0.25

    def delay(self, retry_index: int) -> float:
        base = self.backoff_s[min(retry_index, len(self.backoff_s) - 1)]
        return base * (1 + random.uniform(-self.jitter, self.jitter))  # noqa: S311 - jitter, not crypto


NO_RETRY = RetryPolicy(attempts=1)


def host_allowed(url: str, allow_hosts: Iterable[str]) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    host = parts.hostname.lower()
    return host in {h.lower() for h in allow_hosts}


def build_client(
    *,
    allow_hosts: Iterable[str],
    proxy_url: str | None,
    cookies: httpx.Cookies | dict[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Client bound to one connection's allowlist. `transport` is for tests only."""
    allowed = frozenset(h.lower() for h in allow_hosts)

    async def _guard(request: httpx.Request) -> None:
        if request.url.host.lower() not in allowed or request.url.scheme != "https":
            raise BotaiError(
                ErrorCode.EGRESS_BLOCKED,
                f"Хост {request.url.host} не входит в разрешённые для этого подключения",
                extra={"host": request.url.host},
            )

    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        proxy=proxy_url,
        cookies=cookies,
        headers={"user-agent": "BotAI/0.1 (+https://botai.mnema.app)", **(headers or {})},
        follow_redirects=True,
        max_redirects=5,
        event_hooks={"request": [_guard]},
        transport=transport,
    )


def _retry_after_s(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _retryable_status(status: int) -> bool:
    return status in (408, 425, 429) or 500 <= status < 600


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    policy: RetryPolicy = RetryPolicy(),
    budget_s: float = TOTAL_BUDGET_S,
    **kwargs: object,
) -> httpx.Response:
    """Send a request with LLD §5 semantics. Never retries 4xx except 408/425/429.

    Raises BotaiError(source_unavailable | source_rate_limited) when retries are exhausted.
    Auth-expiry detection (401/403/login redirect) is the caller's job: those responses are
    returned as-is, not retried.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    last_exc: Exception | None = None
    last_response: httpx.Response | None = None
    for attempt in range(policy.attempts):
        try:
            response = await client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except BotaiError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc, last_response = exc, None
        else:
            if not _retryable_status(response.status_code):
                return response
            last_response, last_exc = response, None
        if attempt == policy.attempts - 1:
            break
        delay = policy.delay(attempt)
        if last_response is not None:
            ra = _retry_after_s(last_response)
            if ra is not None:
                delay = min(max(delay, ra), policy.max_retry_after_s)
        if loop.time() + delay > deadline:
            break
        await asyncio.sleep(delay)
    if last_response is not None and last_response.status_code == 429:
        raise BotaiError(ErrorCode.SOURCE_RATE_LIMITED, "Сайт вуза просит подождать")
    detail = (
        "Сайт вуза не отвечает"
        if last_exc
        else f"Сайт вуза ответил {last_response.status_code if last_response else '?'}"
    )
    raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, detail)
