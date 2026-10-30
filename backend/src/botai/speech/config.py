"""Settings of dictation, kept apart from the application's so that a typo cannot stop the API.

A wrong `STT_*` value (a daily budget under one recording, `STT_LANGUAGE=RU`, a provider that does
not exist) turns dictation off with a warning in the log; everything else starts as usual. The
warning names the settings, never their values.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from botai.settings import Settings

log = logging.getLogger("botai.speech")


class SpeechSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Settings.model_config.get("env_file"),
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
    )

    # Read by the api container only (speech.secrets, deployment.md); never logged or returned.
    groq_api_key: SecretStr = SecretStr("")
    stt_provider: Literal["groq", "off"] = "groq"  # "groq" works only when a key is present
    stt_model: str = Field(default="whisper-large-v3-turbo", min_length=1, max_length=60)
    stt_language: str = Field(default="ru", pattern=r"^[a-z]{2}$")
    stt_deadline_s: float = Field(default=45.0, ge=5, le=120)
    stt_requests_per_hour: int = Field(default=60, ge=1, le=10_000)
    # At least one recording of the longest length, or nobody could ever dictate.
    stt_seconds_per_day: int = Field(default=7200, ge=180, le=86_400)


def load_speech_settings() -> SpeechSettings | None:
    """The dictation settings, or None (with a warning) when one of them is invalid."""
    try:
        return SpeechSettings()
    except ValidationError as exc:
        names = sorted({str(error["loc"][0]).upper() for error in exc.errors() if error["loc"]})
        log.warning("Dictation is off: invalid settings %s", ", ".join(names) or "STT_*")
        return None
