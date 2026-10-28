"""DeepSeekProvider: the `LlmProvider` for the OpenAI-compatible DeepSeek API (ADR-0007).

Owner: W5. This file and `core/http.py` are the only places where a direct `httpx` client is
allowed (`tests/core/test_architecture.py`); the host is pinned to `api.deepseek.com`.

What the provider does, in order, for every `complete()`:

1. resolves the profile (model, `thinking`, `reasoning_effort`) from `purpose` through the
   `llm_profile_*` settings for every field the request leaves unset;
2. checks the user's quota (`botai.usage.service.check_quota`) when a usage scope is active;
3. sends the request (streaming by default: 15 s idle timeout between chunks; non-streaming:
   60 s), retrying twice on 5xx / 429 / timeouts / broken streams;
4. maps `usage` (cache hit / miss, output, reasoning) and records it after success.

The API key is read from `settings.deepseek_api_key` only to build the `Authorization` header.
It is never logged and never part of an exception text. Prompts and answers are never logged.

Who is "the user"? `LlmRequest` carries no user. The job runner (or the harness) wraps work in
`usage_scope(user_id, job_id)`; calls outside a scope (benchmark, tests) are not accounted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import (
    Effort,
    LlmMessage,
    LlmProvider,
    LlmRequest,
    LlmResponse,
    LlmTool,
    LlmToolCall,
    LlmUsage,
    Purpose,
    Thinking,
)
from botai.settings import Settings
from botai.usage import service as usage_service

log = logging.getLogger("botai.agent.llm")

DEEPSEEK_HOST = "api.deepseek.com"
CHAT_PATH = "/chat/completions"

# LLD §5: 60 s per call; for a stream, 15 s between chunks. Two retries on 5xx/429/timeouts.
CALL_TIMEOUT_S = 60.0
STREAM_IDLE_TIMEOUT_S = 15.0
STREAM_TOTAL_CAP_S = 600.0
RETRIES = 2
BACKOFF_S = (1.0, 3.0)
MAX_RETRY_AFTER_S = 20.0

# `max_tokens` bounds answer + reasoning in thinking mode. Callers think in answer tokens, so the
# provider adds headroom for the chain of thought (otherwise `learn` with the default 2000 would
# spend everything on reasoning and return nothing).
REASONING_HEADROOM: dict[str, int] = {"low": 4_000, "high": 12_000, "max": 32_000}
DEFAULT_HEADROOM = REASONING_HEADROOM["high"]  # DeepSeek default effort is high
MAX_OUTPUT_TOKENS = 64_000

_RETRYABLE_STATUS = frozenset({408, 409, 425, 429})


# --- profiles -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Profile:
    model: str
    thinking: Thinking | None
    effort: Effort | None


def parse_profile(raw: str) -> Profile:
    """`model:thinking:effort`, `-` = unset (agent-harness.md §2). Bad config fails at startup."""
    parts = raw.strip().split(":")
    if len(parts) != 3 or not parts[0]:
        raise ValueError(f"LLM profile must look like 'model:thinking:effort', got {raw!r}")
    model, thinking, effort = parts
    if thinking not in ("enabled", "disabled", "-"):
        raise ValueError(f"LLM profile {raw!r}: thinking must be enabled|disabled|-")
    if effort not in ("low", "high", "max", "-"):
        raise ValueError(f"LLM profile {raw!r}: effort must be low|high|max|-")
    return Profile(
        model=model,
        thinking=None if thinking == "-" else thinking,
        effort=None if effort == "-" else effort,
    )


def load_profiles(settings: Settings) -> dict[str, Profile]:
    """Purpose -> profile. `summarize` shares the cheap `classify` profile; `bench` has none."""
    classify = parse_profile(settings.llm_profile_classify)
    return {
        "classify": classify,
        "summarize": classify,
        "chat": parse_profile(settings.llm_profile_chat),
        "learn": parse_profile(settings.llm_profile_learn),
        "repair": parse_profile(settings.llm_profile_repair),
        "extract": parse_profile(settings.llm_profile_extract),
    }


@dataclass(frozen=True, slots=True)
class Resolved:
    model: str
    thinking: Thinking | None
    effort: Effort | None


def resolve(request: LlmRequest, settings: Settings, profiles: dict[str, Profile]) -> Resolved:
    """Request fields win; unset ones come from the purpose profile, then from the tier."""
    profile = profiles.get(request.purpose)
    if request.model:
        model = request.model
    elif request.tier == "pro":
        model = settings.llm_model_pro
    elif profile:
        model = profile.model
    else:
        model = settings.llm_model_fast
    thinking = request.thinking or (profile.thinking if profile else None)
    effort = request.effort or (profile.effort if profile else None)
    return Resolved(model, thinking, effort)


# --- usage scope and accounting -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsageScope:
    user_id: str
    job_id: str | None = None


_scope: ContextVar[UsageScope | None] = ContextVar("botai_llm_usage_scope", default=None)


def current_scope() -> UsageScope | None:
    return _scope.get()


@contextmanager
def usage_scope(user_id: str, job_id: str | None = None) -> Generator[UsageScope]:
    """Attribute every LLM call made inside the block to this user and job (quota + `llm_usage`)."""
    scope = UsageScope(user_id, job_id)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


class UsageGate(Protocol):
    async def check(self, user_id: str) -> None:
        """Raises BotaiError(quota_exceeded) when the user may not spend more."""
        ...

    async def record(
        self, *, user_id: str, job_id: str | None, purpose: str, usage: LlmUsage, peak: bool
    ) -> None: ...


class DbUsageGate:
    """The real gate: W1's `botai.usage.service` in short sessions of its own."""

    def __init__(self, sessionmaker: Callable[[], async_sessionmaker[AsyncSession]]) -> None:
        self._sessionmaker = sessionmaker

    async def check(self, user_id: str) -> None:
        async with self._sessionmaker()() as session:
            await usage_service.check_quota(session, user_id)

    async def record(
        self, *, user_id: str, job_id: str | None, purpose: str, usage: LlmUsage, peak: bool
    ) -> None:
        async with self._sessionmaker()() as session:
            await usage_service.record_usage(
                session, user_id=user_id, job_id=job_id, purpose=purpose, usage=usage, peak=peak
            )
            await session.commit()


def is_peak(at: datetime) -> bool:
    """DeepSeek peak tariff: Mon-Fri 01:00-04:00 and 06:00-10:00 UTC (feasibility.md §3.1).

    Chinese public holidays are not modelled, so the cost may be overestimated up to 2x there.
    """
    at = at.astimezone(UTC)
    return at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)


# --- wire format --------------------------------------------------------------------------------


def _tool_to_wire(tool: LlmTool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _message_to_wire(message: LlmMessage, *, with_reasoning: bool) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name:
        wire["name"] = message.name
    if message.role == "tool":
        wire["tool_call_id"] = message.tool_call_id
    if message.role == "assistant":
        if message.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        if with_reasoning and message.reasoning_content is not None:
            wire["reasoning_content"] = message.reasoning_content
    return wire


def _schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Ответь одним JSON-объектом, строго по этой JSON Schema, без пояснений и без Markdown:\n"
        + json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def build_body(request: LlmRequest, resolved: Resolved, *, stream: bool) -> dict[str, Any]:
    """The JSON body for `POST /chat/completions`. Pure, so the prefix is testable byte for byte.

    `reasoning_content` of earlier assistant turns goes back only when tools are present: with
    tools the API demands it (400 otherwise), without tools it is ignored and only costs tokens.
    """
    with_tools = bool(request.tools)
    messages = [_message_to_wire(m, with_reasoning=with_tools) for m in request.messages]
    if request.json_schema is not None:
        # DeepSeek has `json_object` but no `json_schema`: the schema travels as an instruction
        # right after the leading system messages (stable per purpose, so it stays in the prefix).
        lead = 0
        while lead < len(messages) and messages[lead]["role"] == "system":
            lead += 1
        messages.insert(
            lead, {"role": "system", "content": _schema_instruction(request.json_schema)}
        )
    body: dict[str, Any] = {"model": resolved.model, "messages": messages}

    thinking_on = resolved.thinking != "disabled"
    if resolved.thinking is not None:
        body["thinking"] = {"type": resolved.thinking}
    if thinking_on:
        if resolved.effort is not None:
            body["reasoning_effort"] = resolved.effort
        headroom = REASONING_HEADROOM.get(resolved.effort or "", DEFAULT_HEADROOM)
        body["max_tokens"] = min(request.max_tokens + headroom, MAX_OUTPUT_TOKENS)
    else:
        body["temperature"] = request.temperature  # ignored by the API in thinking mode
        body["max_tokens"] = min(request.max_tokens, MAX_OUTPUT_TOKENS)

    if with_tools:
        body["tools"] = [_tool_to_wire(t) for t in request.tools]
    if request.json_schema is not None:
        body["response_format"] = {"type": "json_object"}
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return body


def _usage_from_wire(raw: dict[str, Any] | None, *, model: str, latency_ms: int) -> LlmUsage:
    raw = raw or {}
    prompt = int(raw.get("prompt_tokens") or 0)
    hit = raw.get("prompt_cache_hit_tokens")
    miss = raw.get("prompt_cache_miss_tokens")
    if hit is None and miss is None:
        hit_n, miss_n = 0, prompt
    else:
        hit_n = int(hit or 0)
        miss_n = int(miss) if miss is not None else max(prompt - hit_n, 0)
    details = raw.get("completion_tokens_details") or {}
    return LlmUsage(
        input_cache_miss=miss_n,
        input_cache_hit=hit_n,
        output=int(raw.get("completion_tokens") or 0),
        reasoning=int(details.get("reasoning_tokens") or 0),
        model=model,
        latency_ms=latency_ms,
    )


def _tool_call_from_wire(raw: dict[str, Any]) -> LlmToolCall:
    fn = raw.get("function") or {}
    text = fn.get("arguments") or "{}"
    try:
        parsed = json.loads(text)
        arguments = parsed if isinstance(parsed, dict) else {"__invalid_arguments__": text[:500]}
    except json.JSONDecodeError:
        arguments = {"__invalid_arguments__": text[:500]}
    return LlmToolCall(
        id=str(raw.get("id") or ""), name=str(fn.get("name") or ""), arguments=arguments
    )


@dataclass(slots=True)
class _Accumulator:
    """Collects streaming deltas into one assistant message."""

    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None

    def feed(self, chunk: dict[str, Any]) -> None:
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self.content.append(delta["content"])
            if delta.get("reasoning_content"):
                self.reasoning.append(delta["reasoning_content"])
            for part in delta.get("tool_calls") or []:
                slot = self.calls.setdefault(
                    int(part.get("index") or 0),
                    {"id": "", "function": {"name": "", "arguments": ""}},
                )
                if part.get("id"):
                    slot["id"] = part["id"]
                fn = part.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def message(self) -> dict[str, Any]:
        return {
            "content": "".join(self.content),
            "reasoning_content": "".join(self.reasoning) or None,
            "tool_calls": [self.calls[i] for i in sorted(self.calls)],
        }


class _Retryable(Exception):
    def __init__(self, reason: str, retry_after_s: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_s = retry_after_s


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "")
    return float(value) if value.replace(".", "", 1).isdigit() else None


def _llm_error(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.LLM_UNAVAILABLE, detail, status=503)


Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]


class DeepSeekProvider:
    """`LlmProvider` over `POST {base_url}/chat/completions`. See the module docstring."""

    def __init__(
        self,
        settings: Settings,
        *,
        gate: UsageGate | None = None,
        stream: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        now: Now = lambda: datetime.now(UTC),
    ) -> None:
        base = urlsplit(settings.llm_base_url)
        if base.scheme != "https" or base.hostname != DEEPSEEK_HOST or base.username:
            raise ValueError(f"LLM base URL must be https://{DEEPSEEK_HOST}, got {base.hostname!r}")
        self._settings = settings
        self._url = settings.llm_base_url.rstrip("/") + CHAT_PATH
        self._profiles = load_profiles(settings)
        self._stream = stream
        self._transport = transport
        self._sleep = sleep
        self._now = now
        self._gate: UsageGate = gate or DbUsageGate(_default_sessionmaker)
        self._client: httpx.AsyncClient | None = None

    # -- public --------------------------------------------------------------------------------

    async def complete(self, request: LlmRequest) -> LlmResponse:
        resolved = resolve(request, self._settings, self._profiles)
        body = build_body(request, resolved, stream=self._stream)
        diagnostic_event(
            "llm.start",
            purpose=request.purpose,
            message_count=len(request.messages),
            tool_count=len(request.tools),
        )
        scope = current_scope()
        if scope is not None:
            await self._gate.check(scope.user_id)  # raises quota_exceeded before any spend

        last: _Retryable | None = None
        for attempt in range(RETRIES + 1):
            started = time.monotonic()
            try:
                response = await self._send_once(body, resolved, started)
            except _Retryable as exc:
                last = exc
                diagnostic_event(
                    "llm.retry",
                    purpose=request.purpose,
                    attempt=attempt + 1,
                    error_code="llm_unavailable",
                )
                log.warning(
                    "llm call failed (attempt %d/%d): %s",
                    attempt + 1,
                    RETRIES + 1,
                    exc.reason,
                    extra={"purpose": request.purpose},
                )
                if attempt == RETRIES:
                    break
                await self._sleep(self._delay(attempt, exc.retry_after_s))
                continue
            await self._account(scope, request.purpose, response.usage)
            diagnostic_event(
                "llm.complete",
                purpose=request.purpose,
                attempt=attempt + 1,
                input_tokens=response.usage.input_cache_hit + response.usage.input_cache_miss,
                output_tokens=response.usage.output,
                reasoning_tokens=response.usage.reasoning,
                elapsed_ms=response.usage.latency_ms,
                tool_count=len(response.tool_calls),
                output_chars=len(response.content),
            )
            return response
        raise _llm_error(
            "Модель сейчас недоступна, я не смог получить ответ. Попробуйте чуть позже."
        ) from last

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- internals -----------------------------------------------------------------------------

    def _delay(self, attempt: int, retry_after_s: float | None) -> float:
        base = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
        delay = base * (1 + random.uniform(-0.25, 0.25))  # noqa: S311 - jitter, not crypto
        if retry_after_s is not None:
            delay = min(max(delay, retry_after_s), MAX_RETRY_AFTER_S)
        return delay

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(
                connect=10.0,
                read=STREAM_IDLE_TIMEOUT_S if self._stream else CALL_TIMEOUT_S,
                write=10.0,
                pool=10.0,
            )
            self._client = httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, transport=self._transport
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        key = self._settings.deepseek_api_key.get_secret_value()
        if not key:
            log.error("DEEPSEEK_API_KEY is empty")
            raise _llm_error("Ключ модели не настроен. Сообщите владельцу.")
        return {
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
            "accept": "text/event-stream" if self._stream else "application/json",
        }

    async def _send_once(
        self, body: dict[str, Any], resolved: Resolved, started: float
    ) -> LlmResponse:
        headers = self._headers()
        try:
            async with asyncio.timeout(STREAM_TOTAL_CAP_S if self._stream else CALL_TIMEOUT_S):
                async with self._http().stream(
                    "POST", self._url, json=body, headers=headers
                ) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        self._raise_for_status(resp)
                    message = await self._read_message(resp)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise _Retryable("timeout") from exc
        except httpx.TransportError as exc:
            raise _Retryable(f"transport error: {type(exc).__name__}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = _usage_from_wire(message["usage"], model=resolved.model, latency_ms=latency_ms)
        return LlmResponse(
            content=message["content"] or "",
            reasoning_content=message["reasoning_content"],
            tool_calls=[_tool_call_from_wire(c) for c in message["tool_calls"]],
            usage=usage,
        )

    def _raise_for_status(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if status in _RETRYABLE_STATUS or status >= 500:
            raise _Retryable(f"http {status}", _retry_after(resp))
        # Provider messages may echo input. Status is sufficient; never retain the error body.
        log.error("llm request rejected: http %d", status)
        diagnostic_event("llm.failed", status=status, error_code="llm_unavailable")
        if status == 401:
            raise _llm_error("Ключ модели отклонён. Сообщите владельцу.")
        if status == 402:
            raise _llm_error("На ключе модели закончились средства. Сообщите владельцу.")
        raise _llm_error("Модель не приняла запрос. Попробуйте переформулировать вопрос.")

    async def _read_message(self, resp: httpx.Response) -> dict[str, Any]:
        """Returns {content, reasoning_content, tool_calls, usage} from either response mode."""
        if not self._stream:
            data = json.loads(await resp.aread())
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            return self._finish(
                {
                    "content": message.get("content") or "",
                    "reasoning_content": message.get("reasoning_content") or None,
                    "tool_calls": message.get("tool_calls") or [],
                },
                data.get("usage"),
                choice.get("finish_reason"),
            )
        acc = _Accumulator()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue  # keep-alive comments and blank separators
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                acc.feed(json.loads(payload))
            except ValueError as exc:
                raise _Retryable("malformed stream chunk") from exc
        return self._finish(acc.message(), acc.usage, acc.finish_reason)

    @staticmethod
    def _finish(
        message: dict[str, Any], usage: dict[str, Any] | None, finish_reason: str | None
    ) -> dict[str, Any]:
        if finish_reason is None:
            raise _Retryable("stream ended without finish_reason")
        if finish_reason == "insufficient_system_resource":
            raise _Retryable("provider is out of resources")
        if finish_reason == "length" and not message["content"] and not message["tool_calls"]:
            raise _llm_error(
                "Модель потратила весь лимит на размышления и не успела ответить. "
                "Задайте вопрос короче или разбейте его на части."
            )
        message["usage"] = usage
        return message

    async def _account(self, scope: UsageScope | None, purpose: Purpose, usage: LlmUsage) -> None:
        peak = is_peak(self._now())
        cost = usage_service.cost_usd(usage, peak=peak)
        log.info(
            "llm call purpose=%s model=%s hit=%d miss=%d out=%d reasoning=%d latency_ms=%d "
            "cost_usd=%.6f",
            purpose,
            usage.model,
            usage.input_cache_hit,
            usage.input_cache_miss,
            usage.output,
            usage.reasoning,
            usage.latency_ms,
            cost,
        )
        if scope is None:
            return
        try:
            await self._gate.record(
                user_id=scope.user_id,
                job_id=scope.job_id,
                purpose=purpose,
                usage=usage,
                peak=peak,
            )
        except Exception:
            # The answer is already paid for; losing the accounting row must not lose the answer.
            log.exception("failed to record llm usage")


def _default_sessionmaker() -> async_sessionmaker[AsyncSession]:
    from botai.db.session import get_sessionmaker

    return get_sessionmaker()


# --- metering (benchmark, live tests) -----------------------------------------------------------


class BudgetExceeded(Exception):
    """The metered cap was reached; no further LLM calls are made."""


@dataclass(slots=True)
class CallRecord:
    purpose: str
    usage: LlmUsage
    cost_usd: float


class MeteredLlm:
    """Wraps any provider: counts calls, tokens and dollars, and stops at a hard cap.

    The call that crosses the cap is returned (it is already paid for) and the next one raises.
    """

    def __init__(
        self, inner: LlmProvider, *, cap_usd: float | None = None, peak: bool | None = None
    ) -> None:
        self._inner = inner
        self._cap = cap_usd
        self._peak = peak
        self.calls: list[CallRecord] = []

    @property
    def spent_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    def usages(self) -> Sequence[LlmUsage]:
        return [c.usage for c in self.calls]

    async def complete(self, request: LlmRequest) -> LlmResponse:
        if self._cap is not None and self.spent_usd >= self._cap:
            raise BudgetExceeded(f"cap ${self._cap:.2f} reached (${self.spent_usd:.4f} spent)")
        response = await self._inner.complete(request)
        peak = is_peak(datetime.now(UTC)) if self._peak is None else self._peak
        self.calls.append(
            CallRecord(
                request.purpose, response.usage, usage_service.cost_usd(response.usage, peak=peak)
            )
        )
        return response
