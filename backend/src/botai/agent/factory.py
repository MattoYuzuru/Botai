"""Owner: W5. Replace the deepseek branch with DeepSeekProvider — keep the signature."""

from __future__ import annotations

from pathlib import Path

from botai.core.llm import LlmProvider, ScriptedLlm
from botai.settings import Settings


def build_llm(settings: Settings) -> LlmProvider:
    if settings.llm_provider == "mock":
        return ScriptedLlm.from_dir(Path(__file__).parents[3] / "tests" / "llm_fixtures")
    raise NotImplementedError("DeepSeekProvider is implemented in workstream W5")
