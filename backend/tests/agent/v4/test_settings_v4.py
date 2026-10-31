"""Settings of the v4 providers: new values, new keys, v3 compatibility (settings.py)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from botai.settings import Settings


def test_llm_provider_accepts_the_v4_values() -> None:
    for provider in ("deepseek", "anthropic", "openai_compat", "mock"):
        assert Settings(_env_file=None, llm_provider=provider).llm_provider == provider  # type: ignore[arg-type]


def test_llm_provider_still_rejects_an_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider="claude")  # type: ignore[arg-type]


def test_defaults_stay_v3_compatible() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_provider == "mock"
    assert settings.deepseek_api_key.get_secret_value() == ""
    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_model_fast == "deepseek-flash"
    assert settings.llm_profile_agent == "deepseek-flash:enabled:high"
    assert settings.agent_engine == "v3"  # the new engine is opt-in until phase 2


def test_anthropic_settings_have_safe_defaults_and_hidden_keys() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.anthropic_base_url == "https://api.anthropic.com"
    assert settings.anthropic_model_fast
    assert settings.anthropic_model_pro
    assert settings.anthropic_api_key.get_secret_value() == ""
    settings = Settings(_env_file=None, anthropic_api_key="sk-ant-FIXTURE")  # type: ignore[call-arg]
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-FIXTURE"
    assert "sk-ant-FIXTURE" not in repr(settings)
    assert "sk-ant-FIXTURE" not in str(settings.model_dump())


def test_openai_compatible_settings_are_read_from_their_own_names() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_openai_base_url == "https://api.openai.com/v1"
    assert settings.llm_openai_api_key.get_secret_value() == ""
    assert settings.llm_openai_model_fast
    assert settings.llm_openai_model_pro


def test_provider_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-FIXTURE")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.test")
    monkeypatch.setenv("ANTHROPIC_MODEL_FAST", "claude-fast-FIXTURE")
    monkeypatch.setenv("ANTHROPIC_MODEL_PRO", "claude-pro-FIXTURE")
    monkeypatch.setenv("LLM_OPENAI_API_KEY", "sk-openai-FIXTURE")
    monkeypatch.setenv("LLM_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("LLM_OPENAI_MODEL_FAST", "llama-fast-FIXTURE")
    monkeypatch.setenv("LLM_OPENAI_MODEL_PRO", "llama-pro-FIXTURE")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_provider == "anthropic"
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-FIXTURE"
    assert settings.anthropic_base_url == "https://proxy.test"
    assert settings.anthropic_model_fast == "claude-fast-FIXTURE"
    assert settings.anthropic_model_pro == "claude-pro-FIXTURE"
    assert settings.llm_openai_api_key.get_secret_value() == "sk-openai-FIXTURE"
    assert settings.llm_openai_base_url == "http://127.0.0.1:11434/v1"
    assert settings.llm_openai_model_fast == "llama-fast-FIXTURE"
    assert settings.llm_openai_model_pro == "llama-pro-FIXTURE"


def test_v3_settings_are_not_broken_by_the_new_fields() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        env="test",
        llm_provider="deepseek",
        deepseek_api_key="sk-deepseek-FIXTURE",
        llm_base_url="https://api.deepseek.com",
        llm_model_fast="deepseek-flash",
        llm_model_pro="deepseek-v4-pro",
        llm_profile_agent="deepseek-flash:enabled:high",
    )
    assert settings.llm_provider == "deepseek"
    assert settings.deepseek_api_key.get_secret_value() == "sk-deepseek-FIXTURE"
    assert settings.llm_base_url == "https://api.deepseek.com"
