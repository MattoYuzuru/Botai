"""DeepSeek provider for v4: a thin adapter over the proven `agent.llm.DeepSeekProvider`.

The v3 provider already owns the wire format, streaming, retries, quota scopes and usage
accounting; duplicating that HTTP code in v4 would fork a hardened path. The adapter only adds
`Provider.name` and keeps the existing behaviour exactly (docs/v4/02-architecture.md §10).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from botai.agent.llm import DeepSeekProvider, UsageGate
from botai.core.llm import LlmRequest, LlmResponse
from botai.settings import Settings


class DeepSeekProviderAdapter:
    """`Provider` with `name="deepseek"`; all LLM logic stays in `botai.agent.llm`."""

    def __init__(
        self,
        settings: Settings,
        *,
        gate: UsageGate | None = None,
        stream: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "gate": gate,
            "stream": stream,
            "transport": transport,
            "sleep": sleep if sleep is not None else asyncio.sleep,
            "now": now if now is not None else (lambda: datetime.now(UTC)),
        }
        self._inner = DeepSeekProvider(settings, **kwargs)

    @property
    def name(self) -> str:
        return "deepseek"

    async def complete(self, request: LlmRequest) -> LlmResponse:
        return await self._inner.complete(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


__all__ = ["DeepSeekProviderAdapter"]
