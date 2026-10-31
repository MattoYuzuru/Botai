"""Shared HTTP retry loop for the v4 providers (Anthropic, OpenAI-compatible).

Two retries on 5xx/429/timeouts/broken JSON, exponential backoff honouring `Retry-After`
(architecture §4.7). The call is bounded by a total timeout, so a provider that trickles bytes
cannot outlive it. Error bodies are never retained: provider messages may echo the prompt, so the
status code is enough (the key is never part of exception text either).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from botai.core.errors import BotaiError, ErrorCode

RETRIES = 2
BACKOFF_S = (1.0, 3.0)
MAX_RETRY_AFTER_S = 20.0
RETRYABLE_STATUS = frozenset({408, 409, 425, 429})
TOTAL_TIMEOUT_S = 180.0

Sleep = Callable[[float], Awaitable[None]]


def llm_error(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.LLM_UNAVAILABLE, detail, status=503)


def rejected_error(status: int) -> BotaiError:
    """A non-retryable status as a user-facing error without the response body."""
    if status == 401:
        return llm_error("Ключ модели отклонён. Сообщите владельцу.")
    if status == 402:
        return llm_error("На ключе модели закончились средства. Сообщите владельцу.")
    return llm_error("Модель не приняла запрос. Попробуйте переформулировать вопрос.")


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "")
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _delay(attempt: int, retry_after_s: float | None) -> float:
    delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
    if retry_after_s is not None:
        delay = min(max(delay, retry_after_s), MAX_RETRY_AFTER_S)
    return delay


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    timeout_s: float = TOTAL_TIMEOUT_S,
    retries: int = RETRIES,
    sleep: Sleep = asyncio.sleep,
) -> tuple[dict[str, Any], int]:
    """POST JSON and return `(decoded body, latency_ms)`; raises `BotaiError(llm_unavailable)`."""
    last_reason = "unknown"
    for attempt in range(retries + 1):
        started = time.monotonic()
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(timeout_s):
                response = await client.post(url, json=dict(body), headers=dict(headers))
        except (TimeoutError, httpx.TimeoutException):
            last_reason = "timeout"
        except httpx.TransportError as exc:
            last_reason = f"transport error: {type(exc).__name__}"
        else:
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    last_reason = "malformed response body"
                else:
                    if isinstance(payload, dict):
                        return payload, int((time.monotonic() - started) * 1000)
                    last_reason = "malformed response body"
            elif response.status_code in RETRYABLE_STATUS or response.status_code >= 500:
                last_reason = f"http {response.status_code}"
            else:
                raise rejected_error(response.status_code)
        if attempt == retries:
            break
        retry_after = _retry_after(response) if response is not None else None
        await sleep(_delay(attempt, retry_after))
    raise llm_error(
        "Модель сейчас недоступна, я не смог получить ответ. Попробуйте чуть позже."
    ) from _ProviderRetry(last_reason)


class _ProviderRetry(Exception):
    """Internal marker kept for the `raise ... from` chain; its text is not shown to users."""


__all__ = [
    "BACKOFF_S",
    "RETRIES",
    "TOTAL_TIMEOUT_S",
    "Sleep",
    "llm_error",
    "post_json",
    "rejected_error",
]
