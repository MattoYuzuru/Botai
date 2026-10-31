"""Settings: a renamed variable is refused loudly, not ignored; soft limits are opt-in."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

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


# --- v4 engine flag and soft limits ---------------------------------------------------------


def test_the_agent_engine_is_v3_until_the_flag_says_v4(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).agent_engine == "v3"  # type: ignore[call-arg]
    monkeypatch.setenv("BOTAI_AGENT_ENGINE", "v4")
    assert Settings(_env_file=None).agent_engine == "v4"  # type: ignore[call-arg]
    monkeypatch.setenv("BOTAI_AGENT_ENGINE", "v5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_soft_limits_are_off_by_default_and_have_an_emergency_stop() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.agent_limits_steps is None
    assert settings.agent_limits_seconds is None
    assert settings.agent_limits_tokens is None
    assert settings.agent_limits_usd is None
    assert settings.agent_hard_stop_usd == 2.0
    assert settings.agent_auto_continue is False


def test_soft_limits_are_read_from_their_own_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (
        ("BOTAI_AGENT_LIMITS_STEPS", "12"),
        ("BOTAI_AGENT_LIMITS_SECONDS", "90.5"),
        ("BOTAI_AGENT_LIMITS_TOKENS", "250000"),
        ("BOTAI_AGENT_LIMITS_USD", "1.25"),
        ("BOTAI_AGENT_HARD_STOP_USD", "3"),
        ("BOTAI_AGENT_AUTO_CONTINUE", "true"),
    ):
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.agent_limits_steps == 12
    assert settings.agent_limits_seconds == 90.5
    assert settings.agent_limits_tokens == 250_000
    assert settings.agent_limits_usd == 1.25
    assert settings.agent_hard_stop_usd == 3.0
    assert settings.agent_auto_continue is True


@pytest.mark.parametrize(
    "field",
    [
        {"agent_limits_steps": 0},
        {"agent_limits_seconds": 0},
        {"agent_limits_tokens": 0},
        {"agent_limits_usd": 0},
        {"agent_hard_stop_usd": 0},
        {"botai_research_no_progress_slices": 0},
    ],
)
def test_limits_reject_values_that_would_stop_every_task_at_once(field: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **field)  # type: ignore[call-arg]


# --- deprecated v3 research limits ----------------------------------------------------------


def test_the_legacy_research_limits_still_work_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOTAI_RESEARCH_MAX_STEPS", "42")
    monkeypatch.setenv("BOTAI_RESEARCH_MAX_REQUESTS", "17")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.botai_research_max_steps == 42
    assert settings.botai_research_max_requests == 17


def test_legacy_research_settings_are_reported_as_deprecated_at_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("BOTAI_RESEARCH_MAX_STEPS", "42")
    try:
        with caplog.at_level(logging.WARNING, logger="botai.settings"):
            settings = get_settings()
        assert settings.botai_research_max_steps == 42  # still honored, not refused
        assert "BOTAI_RESEARCH_MAX_STEPS" in caplog.text
        assert "agent_limits" in caplog.text
    finally:
        get_settings.cache_clear()


def test_no_progress_stop_defaults_to_three_slices() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.botai_research_no_progress_slices == 3
    assert (
        Settings(
            _env_file=None, botai_research_no_progress_slices=5
        ).botai_research_no_progress_slices  # type: ignore[call-arg]
        == 5
    )
