"""The provider protocol of the v4 runtime (docs/v4/02-architecture.md §10).

`Provider` is structural: it takes the shared `core.llm.LlmRequest` and returns
`core.llm.LlmResponse`, so v4 providers, the existing `DeepSeekProvider` and `ScriptedLlm` are
interchangeable. The runtime never imports a concrete provider; the wiring chooses one by
settings (`botai.agent.v4.providers.factory.build_provider`).

Primary secrets (API keys) are read only to build an auth header; they never appear in logs,
exceptions or trace payloads.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr

from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import LlmRequest, LlmResponse


def require_secret(value: SecretStr, *, provider: str) -> str:
    """The key to put in an auth header, or a secret-free `llm_unavailable` error."""
    secret = value.get_secret_value()
    if not secret:
        raise BotaiError(
            ErrorCode.LLM_UNAVAILABLE,
            f"Ключ модели {provider} не настроен. Сообщите владельцу.",
            status=503,
        )
    return secret


class Provider(Protocol):
    """Anything that turns an `LlmRequest` into an `LlmResponse`."""

    @property
    def name(self) -> str: ...

    async def complete(self, request: LlmRequest) -> LlmResponse: ...

    async def aclose(self) -> None: ...


__all__ = ["Provider", "require_secret"]
