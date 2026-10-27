"""Owner: W5. Builds the LLM provider for the service container (`botai.wiring`)."""

from __future__ import annotations

from pathlib import Path

from botai.agent.llm import DeepSeekProvider
from botai.core.llm import LlmProvider, ScriptedLlm
from botai.settings import Settings


def build_llm(settings: Settings) -> LlmProvider:
    if settings.llm_provider == "mock":
        return ScriptedLlm.from_dir(Path(__file__).parents[3] / "tests" / "llm_fixtures")
    return DeepSeekProvider(settings)
