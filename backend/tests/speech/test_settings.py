"""Dictation settings: defaults, bounds, a key that stays secret, and a typo that must not stop the API."""

from __future__ import annotations

import logging
from collections.abc import Generator

import httpx
import pytest
from pydantic import ValidationError

from botai.main import create_app
from botai.settings import Settings
from botai.speech import router
from botai.speech.config import SpeechSettings, load_speech_settings
from botai.speech.service import build_speech_service

KEY = "gsk_test_FIXTURE_key_0123456789"


def make(**over: object) -> SpeechSettings:
    return SpeechSettings(_env_file=None, **over)  # type: ignore[call-arg]


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
        {"stt_language": "RU"},
        {"stt_seconds_per_day": 60},  # less than one recording of the longest length
        {"stt_requests_per_hour": 0},
        {"stt_deadline_s": 1},
    ],
)
def test_nonsense_values_are_refused_by_the_dictation_settings(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make(**bad)


@pytest.fixture
def fresh_service() -> Generator[None]:
    router._service.cache_clear()  # pyright: ignore[reportPrivateUsage]
    yield
    router._service.cache_clear()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("STT_SECONDS_PER_DAY", "60"),
        ("STT_LANGUAGE", "RU"),
        ("STT_PROVIDER", "whisper"),
        ("STT_REQUESTS_PER_HOUR", "lots"),
        ("STT_DEADLINE_S", "soon"),
    ],
)
async def test_a_typo_turns_dictation_off_with_a_warning_and_the_api_still_starts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fresh_service: None,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    monkeypatch.setenv(name, value)
    app = create_app()  # does not raise
    # The application's own logging setup replaces the handlers, so the warning is read from stderr.
    warning = capsys.readouterr().err
    assert "Dictation is off" in warning
    assert name in warning  # the setting is named...
    assert value not in warning  # ...its value never is
    assert KEY not in warning
    service = build_speech_service(Settings(_env_file=None))  # type: ignore[call-arg]
    assert not service.available
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        health = await client.get("/api/health")
        assert health.status_code == 200
        off = await client.post("/api/v1/speech/transcribe", files={"audio": ("a.webm", b"x")})
        assert (off.status_code, off.json()["code"]) == (503, "stt_unavailable")


def test_valid_settings_load_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    monkeypatch.setenv("STT_SECONDS_PER_DAY", "3600")
    with caplog.at_level(logging.WARNING, logger="botai.speech"):
        loaded = load_speech_settings()
    assert loaded is not None
    assert loaded.stt_seconds_per_day == 3600
    assert [r for r in caplog.records if r.name == "botai.speech"] == []
    assert build_speech_service(Settings(_env_file=None)).available  # type: ignore[call-arg]
