"""Fixtures for the agent tests. L0/L1 never read the real key: settings ignore every env file."""

from __future__ import annotations

import os

import pytest

from botai.settings import Settings

TEST_KEY = "sk-test-DO-NOT-LEAK-0123456789"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "llm_provider": "deepseek",
        "deepseek_api_key": TEST_KEY,
        "env": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """L2 (`live_llm`) spends real money: explicit opt-in only, otherwise skipped with a reason."""
    if os.environ.get("BOTAI_LIVE_LLM") == "1":
        return
    skip = pytest.mark.skip(reason="L2: set BOTAI_LIVE_LLM=1 (and BOTAI_ENV_FILE) to spend money")
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip)
