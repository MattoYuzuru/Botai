"""MockProvider: a `Provider` over `core.llm.ScriptedLlm` (L0 tests, benchmark fixtures).

Reuses the v3 scripted double instead of a second fake: a queue of `LlmResponse`s or responses
keyed by the request fingerprint. An unknown request fails loudly (`LlmFixtureMissing`) — a silent
empty answer would hide bugs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from botai.core.llm import LlmRequest, LlmResponse, ScriptedLlm


class MockProvider:
    """Scripted provider for tests; `requests` exposes what the runtime sent."""

    def __init__(
        self,
        responses: Sequence[LlmResponse] = (),
        *,
        by_fingerprint: Mapping[str, LlmResponse] | None = None,
    ) -> None:
        self._scripted = ScriptedLlm(by_fingerprint=by_fingerprint, queue=list(responses))

    @property
    def name(self) -> str:
        return "mock"

    @property
    def requests(self) -> list[LlmRequest]:
        return self._scripted.requests

    @classmethod
    def from_responses(cls, *responses: LlmResponse) -> MockProvider:
        return cls(responses=list(responses))

    async def complete(self, request: LlmRequest) -> LlmResponse:
        return await self._scripted.complete(request)

    async def aclose(self) -> None:
        return None


__all__ = ["MockProvider"]
