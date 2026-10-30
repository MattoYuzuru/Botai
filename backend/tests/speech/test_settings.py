"""Dictation settings: defaults, bounds, and a key that stays secret."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from botai.settings import Settings

KEY = "gsk_test_FIXTURE_key_0123456789"


def make(**over: object) -> Settings:
    return Settings(_env_file=None, **over)  # type: ignore[call-arg]


def test_defaults_are_groq_turbo_in_russian_with_a_modest_quota() -> None:
    s = make()
    assert (s.stt_provider, s.stt_model, s.stt_language) == ("groq", "whisper-large-v3-turbo", "ru")
    assert (s.stt_requests_per_hour, s.stt_seconds_per_day) == (60, 7200)
    assert s.groq_api_key.get_secret_value() == ""


def test_the_key_is_read_from_the_environment_and_never_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    monkeypatch.setenv("STT_PROVIDER", "off")
    s = make()
    assert s.groq_api_key.get_secret_value() == KEY
    assert s.stt_provider == "off"
    assert KEY not in repr(s) + str(s) + s.model_dump_json()


@pytest.mark.parametrize(
    "bad",
    [
        {"stt_provider": "whisper"},
        {"stt_language": "russian"},
        {"stt_seconds_per_day": 60},  # less than one recording of the longest length
        {"stt_requests_per_hour": 0},
        {"stt_deadline_s": 1},
    ],
)
def test_nonsense_values_stop_the_start(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make(**bad)
