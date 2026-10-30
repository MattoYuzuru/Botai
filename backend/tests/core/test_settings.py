"""Settings: a renamed variable is refused loudly, not ignored."""

from __future__ import annotations

import pytest

from botai.settings import Settings, get_settings


def test_the_old_chat_profile_name_stops_the_start_instead_of_being_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("LLM_PROFILE_CHAT", "deepseek-flash:enabled:high")
    try:
        with pytest.raises(RuntimeError, match="LLM_PROFILE_AGENT"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_the_agent_profile_is_read_from_its_own_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROFILE_AGENT", "deepseek-flash:enabled:high")
    assert Settings(_env_file=None).llm_profile_agent == "deepseek-flash:enabled:high"  # type: ignore[call-arg]
