"""Choose a v4 provider by settings (D-012). Wiring calls `build_provider`, code never does.

Import this module explicitly (`from botai.agent.v4.providers.factory import build_provider`):
it pulls the DeepSeek adapter, which reuses the v3 provider and with it the DB-backed usage
module. `botai.agent.v4.providers` itself stays light so the runtime remains importable without
database modules.
"""

from __future__ import annotations

from botai.agent.v4.providers.anthropic import AnthropicProvider
from botai.agent.v4.providers.base import Provider
from botai.agent.v4.providers.deepseek import DeepSeekProviderAdapter
from botai.agent.v4.providers.mock import MockProvider
from botai.agent.v4.providers.openai_compat import OpenAICompatProvider
from botai.settings import Settings


def build_provider(settings: Settings) -> Provider:
    """Provider for `settings.llm_provider`; never reads a key until the first request."""
    if settings.llm_provider == "mock":
        return MockProvider()
    if settings.llm_provider == "deepseek":
        return DeepSeekProviderAdapter(settings)
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings)
    if settings.llm_provider == "openai_compat":
        return OpenAICompatProvider(settings)
    raise ValueError(f"unknown llm_provider: {settings.llm_provider!r}")


__all__ = ["build_provider"]
