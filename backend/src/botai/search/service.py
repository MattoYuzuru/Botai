"""Bounded public Web Search: Exa first, the raw DeepSeek Search API as fallback.

The two fixed provider endpoints use their own EgressProxy grants. Search results never alter
the hosts of a university connection. There is no LLM call, arbitrary fetch, redirect or cookie.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.browser.egress import proxy_url_for
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.core.http import build_client, request_once_capped
from botai.core.ids import new_id
from botai.db.models import LlmUsageRow
from botai.search.safety import public_query, safe_result_url
from botai.settings import Settings
from botai.usage.service import check_quota

Provider = Literal["exa", "deepseek"]
ENDPOINTS: dict[Provider, str] = {
    "exa": "https://api.exa.ai/search",
    "deepseek": "https://api.deepseek.com/v1/tools/search",
}
# Exa lite, <=10 hits, highlights included; DeepSeek per successful non-empty request.
# Per-turn reservation charges every attempt conservatively, including ambiguous timeouts.
COST_USD: dict[Provider, float] = {"exa": 0.004, "deepseek": 0.00075}
MAX_RESPONSE_BYTES = 256 * 1024
MAX_RESULTS = 5
MAX_ATTEMPTS = 2
ATTEMPT_TIMEOUT_S = 5.0
MAX_RETRY_AFTER_S = 3600.0


@dataclass(frozen=True, slots=True)
class SearchResult:
    url: str
    title: str
    snippet: str
    provider: Provider
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class SearchIssue:
    provider: str
    reason: str


@dataclass(frozen=True, slots=True)
class SearchResponse:
    status: Literal["ok", "empty", "unavailable", "rejected", "budget"]
    results: tuple[SearchResult, ...] = ()
    issues: tuple[SearchIssue, ...] = ()


@dataclass(slots=True)
class SearchBudget:
    max_calls: int = 4
    max_cost_usd: float = 0.03
    seconds: float = 15
    calls: int = 0
    reserved_usd: float = 0
    deadline: float | None = None
    queries: set[str] = field(default_factory=set, repr=False)

    def remaining(self, now: float) -> float:
        if self.deadline is None:
            self.deadline = now + self.seconds
        return max(0.0, self.deadline - now)

    def reserve(self, provider: Provider, now: float) -> str | None:
        if self.remaining(now) <= 0:
            return "deadline"
        if self.calls >= self.max_calls:
            return "call_budget"
        if self.reserved_usd + COST_USD[provider] > self.max_cost_usd + 1e-10:
            return "cost_budget"
        self.calls += 1
        self.reserved_usd += COST_USD[provider]
        return None


@dataclass(slots=True)
class _ProviderState:
    until: float = 0
    reason: str = "cooldown"
    disabled: bool = False


Sender = Callable[[Provider, dict[str, Any], float], Awaitable[httpx.Response]]


class SearchService:
    def __init__(
        self,
        settings: Settings,
        *,
        db: async_sessionmaker[AsyncSession] | None = None,
        sender: Sender | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.db = db
        self._sender = sender or self._send
        self._now = monotonic
        self._sleep = sleep
        self._states = {p: _ProviderState() for p in ENDPOINTS}

    def budget(self) -> SearchBudget:
        return SearchBudget(
            max_calls=self.settings.search_max_calls,
            max_cost_usd=self.settings.search_max_cost_usd,
            seconds=self.settings.search_deadline_s,
        )

    def _key(self, provider: Provider) -> str:
        key = self.settings.exa_api_key if provider == "exa" else self.settings.deepseek_api_key
        return key.get_secret_value().strip()

    async def _send(
        self, provider: Provider, body: dict[str, Any], remaining: float
    ) -> httpx.Response:
        host = "api.exa.ai" if provider == "exa" else "api.deepseek.com"
        proxy = await proxy_url_for(self.settings, [host], ttl_s=max(30, remaining + 5))
        headers = (
            {"x-api-key": self._key(provider)}
            if provider == "exa"
            else {"Authorization": f"Bearer {self._key(provider)}"}
        )
        async with build_client(allow_hosts=[host], proxy_url=proxy, headers=headers) as client:
            return await request_once_capped(
                client,
                "POST",
                ENDPOINTS[provider],
                budget_s=remaining,
                max_bytes=MAX_RESPONSE_BYTES,
                follow_redirects=False,
                json=body,
            )

    async def _account(
        self, provider: Provider, *, user_id: str | None, job_id: str | None, before: bool
    ) -> None:
        if self.db is None or user_id is None:
            return
        async with self.db() as db:
            if before:
                await check_quota(db, user_id, settings=self.settings)
            else:
                db.add(
                    LlmUsageRow(
                        id=new_id(),
                        user_id=user_id,
                        job_id=job_id,
                        purpose="search",
                        model=f"search:{provider}",
                        input_cache_miss=0,
                        input_cache_hit=0,
                        output=0,
                        cost_usd=COST_USD[provider],
                        latency_ms=0,
                        created_at=datetime.now(UTC),
                    )
                )
                await db.commit()

    async def search(
        self,
        query: str,
        *,
        budget: SearchBudget | None = None,
        user_id: str | None = None,
        job_id: str | None = None,
    ) -> SearchResponse:
        budget = budget or self.budget()
        clean = public_query(query)
        if clean is None:
            diagnostic_event("search.rejected", reason="unsafe_query")
            return SearchResponse("rejected", issues=(SearchIssue("none", "unsafe_query"),))
        mode = self.settings.search_mode
        if mode == "off":
            diagnostic_event("search.disabled", reason="disabled")
            return SearchResponse("unavailable", issues=(SearchIssue("none", "disabled"),))
        if clean.casefold() in budget.queries:
            return SearchResponse("budget", issues=(SearchIssue("none", "duplicate_query"),))
        budget.queries.add(clean.casefold())
        providers: tuple[Provider, ...] = ("exa", "deepseek") if mode == "auto" else (mode,)
        issues: list[SearchIssue] = []
        for index, provider in enumerate(providers):
            state = self._states[provider]
            if not self._key(provider):
                issues.append(SearchIssue(provider, "missing_key"))
                diagnostic_event("search.skipped", provider=provider, reason="missing_key")
                continue
            if state.disabled or state.until > self._now():
                issues.append(SearchIssue(provider, state.reason))
                diagnostic_event("search.skipped", provider=provider, reason=state.reason)
                continue
            diagnostic_event("search.selected", provider=provider, outcome="started")
            if index:
                diagnostic_event("search.fallback", provider=provider, reason=issues[-1].reason)
            body: dict[str, Any] = (
                {
                    "query": clean,
                    "type": "lite",
                    "numResults": MAX_RESULTS,
                    "contents": {"highlights": True},
                }
                if provider == "exa"
                else {"query": clean, "count": MAX_RESULTS, "need_content": False}
            )
            for attempt in range(MAX_ATTEMPTS):
                limited = budget.reserve(provider, self._now())
                if limited:
                    issues.append(SearchIssue(provider, limited))
                    diagnostic_event("search.limited", provider=provider, reason=limited)
                    return SearchResponse("budget", issues=tuple(issues))
                retry_after = 0.0
                try:
                    # Includes proxy credential acquisition, connection, complete bounded body.
                    remaining = min(ATTEMPT_TIMEOUT_S, budget.remaining(self._now()))
                    async with asyncio.timeout(remaining):
                        await self._account(provider, user_id=user_id, job_id=job_id, before=True)
                        response = await self._sender(provider, body, remaining)
                    reason = _error_reason(response)
                    retry_after = _retry_after(response)
                    if reason is None:
                        state.until = 0
                        state.reason = "cooldown"
                        results = _results(response, provider)
                        # Filtered unsafe hits can still be billable at the provider.
                        if provider == "exa" or response.json()["results"]:
                            await self._account(
                                provider, user_id=user_id, job_id=job_id, before=False
                            )
                        diagnostic_event(
                            "search.result",
                            provider=provider,
                            outcome="ok" if results else "empty",
                            item_count=len(results),
                            attempt=attempt + 1,
                        )
                        return SearchResponse("ok" if results else "empty", results, tuple(issues))
                except BotaiError as exc:
                    if exc.code == ErrorCode.QUOTA_EXCEEDED:
                        raise
                    reason = "timeout" if budget.remaining(self._now()) <= 0 else "unavailable"
                except (TimeoutError, httpx.TransportError):
                    reason = "timeout"
                except (ValueError, TypeError, KeyError):
                    reason = "invalid_response"
                issues.append(SearchIssue(provider, reason))
                diagnostic_event(
                    "search.failed", provider=provider, reason=reason, attempt=attempt + 1
                )
                state.reason = reason
                if reason in {"invalid_key", "invalid_config", "credits_exhausted"}:
                    state.disabled = True  # configuration/top-up requires an operator action
                    break
                # One-way fallback: never return to a provider during this search/turn.
                cooldown = max(self.settings.search_cooldown_s, retry_after)
                state.until = self._now() + cooldown
                if reason == "rate_limited":
                    break  # honor Retry-After as cooldown; fallback need not wait on that host
                delay = max(0.25 * (2**attempt), retry_after)
                if (
                    attempt + 1 >= MAX_ATTEMPTS
                    or reason not in {"timeout", "unavailable"}
                    or delay + 0.1 >= budget.remaining(self._now())
                ):
                    break
                diagnostic_event(
                    "search.retry", provider=provider, reason=reason, delay_ms=round(delay * 1000)
                )
                await self._sleep(delay)
        return SearchResponse("unavailable", issues=tuple(issues))


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after", "")
    try:
        seconds = float(raw)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return min(MAX_RETRY_AFTER_S, max(0.0, seconds))


def _error_reason(response: httpx.Response) -> str | None:
    status = response.status_code
    if status == 200:
        return None
    if status == 402:
        return "credits_exhausted"
    if status in {401, 403}:
        return "invalid_key"
    if status == 429:
        return "rate_limited"
    if status in {408, 425} or status >= 500:
        return "unavailable"
    return "invalid_config"  # includes redirects: do not forward credentials


def _results(response: httpx.Response, provider: Provider) -> tuple[SearchResult, ...]:
    payload: Any = json.loads(response.content)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("invalid search response")
    result: list[SearchResult] = []
    seen: set[str] = set()
    now = datetime.now(UTC)
    for item in payload["results"][:20]:
        if not isinstance(item, dict):
            raise ValueError("invalid search result")
        url = safe_result_url(item.get("url"))
        title = item.get("title")
        if not url or not isinstance(title, str) or url in seen:
            continue
        snippet = item.get("snippet", "")
        if provider == "exa":
            highlights = item.get("highlights", [])
            snippet = (
                " ".join(h for h in highlights[:3] if isinstance(h, str))
                if isinstance(highlights, list)
                else ""
            )
        if not isinstance(snippet, str):
            raise ValueError("invalid search snippet")
        seen.add(url)
        result.append(SearchResult(url, title[:200], snippet[:1000], provider, now))
        if len(result) >= MAX_RESULTS:
            break
    return tuple(result)
