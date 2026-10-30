"""Dictation: a recording in, the words out. The recording is held in memory for the length of one
request and is never stored, logged or turned into a chat attachment.

Order of checks: a service that is off answers first (nothing is read), then the shape of the
recording, then the user's quota, and only then the provider is called.
"""

from __future__ import annotations

import math
import re
import time

from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.settings import Settings
from botai.speech.audio import sniff_audio
from botai.speech.groq import GroqSpeechToText
from botai.speech.provider import SpeechFailure, SpeechToText
from botai.speech.quota import QuotaRefused, SpeechQuota

MAX_AUDIO_BYTES = 20 * 1024 * 1024  # the provider's own limit is higher; this one is ours
MAX_AUDIO_SECONDS = 180  # three minutes: the client stops there, the server checks the claim
DURATION_SLACK_S = 10  # a recorder's last chunk and the clock of the page are not exact
MAX_TEXT_CHARS = 16_000
# A recording that claims to be shorter than its size allows is charged by its size: 40 kB/s is
# well above any bitrate a browser writes (Opus ~4-16 kB/s, AAC ~8-16 kB/s, 16 kHz WAV 32 kB/s).
MAX_PLAUSIBLE_BYTES_PER_S = 40_000
# Without a claim from the client, a recording is charged at a typical bitrate of 64 kbit/s.
TYPICAL_BYTES_PER_S = 8_000
_LANGUAGE = re.compile(r"^[a-z]{2}$")


def build_provider(settings: Settings) -> SpeechToText | None:
    """The configured provider, or None when dictation is off or has no key."""
    if settings.stt_provider == "groq" and settings.groq_api_key.get_secret_value().strip():
        return GroqSpeechToText(settings)
    return None


class SpeechService:
    def __init__(
        self,
        settings: Settings,
        *,
        provider: SpeechToText | None = None,
        quota: SpeechQuota | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider if provider is not None else build_provider(settings)
        self.quota = quota or SpeechQuota(
            per_hour=settings.stt_requests_per_hour, seconds_per_day=settings.stt_seconds_per_day
        )

    @property
    def available(self) -> bool:
        return self.provider is not None

    async def transcribe(
        self,
        user_id: str,
        audio: bytes,
        *,
        language: str | None = None,
        duration_ms: int | None = None,
    ) -> str:
        provider = self.provider
        if provider is None:
            diagnostic_event("speech.refused", outcome="refused", reason="disabled")
            raise BotaiError(
                ErrorCode.STT_UNAVAILABLE,
                "Распознавание речи сейчас не работает. Напишите текстом.",
            )
        mime = self._check(audio, language, duration_ms)
        language = language or self.settings.stt_language
        seconds = _charge_seconds(len(audio), duration_ms)
        started = time.monotonic()
        try:
            with self.quota.slot(user_id, seconds):
                text = await provider.transcribe(audio, mime=mime, language=language)
        except QuotaRefused as exc:
            diagnostic_event("speech.refused", outcome="refused", reason="budget_exceeded")
            raise _quota_error(exc) from None
        except SpeechFailure as exc:
            diagnostic_event(
                "speech.transcribed",
                provider=provider.name,
                outcome="failed",
                body_bytes=len(audio),
                latency_ms=_ms_since(started),
            )
            raise _failure_error(exc) from None
        text = text.strip()[:MAX_TEXT_CHARS]
        diagnostic_event(
            "speech.transcribed",
            provider=provider.name,
            outcome="ok" if text else "empty",
            body_bytes=len(audio),
            text_chars=len(text),
            latency_ms=_ms_since(started),
        )
        return text

    def _check(self, audio: bytes, language: str | None, duration_ms: int | None) -> str:
        if not audio:
            raise BotaiError(ErrorCode.VALIDATION, "Запись пустая. Запишите ещё раз.")
        if len(audio) > MAX_AUDIO_BYTES:
            raise BotaiError(ErrorCode.VALIDATION, "Запись больше 20 МБ.", status=413)
        if duration_ms is not None and duration_ms > (MAX_AUDIO_SECONDS + DURATION_SLACK_S) * 1000:
            raise BotaiError(ErrorCode.VALIDATION, "Запись длиннее трёх минут.", status=413)
        if language is not None and not _LANGUAGE.fullmatch(language):
            raise BotaiError(ErrorCode.VALIDATION, "Неизвестный язык записи.")
        mime = sniff_audio(audio)
        if mime is None:
            raise BotaiError(ErrorCode.VALIDATION, "Не удалось разобрать запись. Запишите ещё раз.")
        return mime


def _charge_seconds(size: int, duration_ms: int | None) -> float:
    floor = size / MAX_PLAUSIBLE_BYTES_PER_S
    claimed = size / TYPICAL_BYTES_PER_S if duration_ms is None else duration_ms / 1000
    return float(min(max(1.0, claimed, floor), 3600))


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _quota_error(exc: QuotaRefused) -> BotaiError:
    minutes = max(1, math.ceil(exc.retry_after_s / 60))
    detail = {
        "busy": "Предыдущая запись ещё распознаётся. Подождите секунду.",
        "hour": f"Слишком много записей за час. Попробуйте через {minutes} мин или напишите текстом.",
        "day": "На сегодня лимит голосового ввода исчерпан. Напишите текстом.",
    }[exc.scope]
    return BotaiError(
        ErrorCode.QUOTA_EXCEEDED,
        detail,
        extra={"scope": f"speech_{exc.scope}", "retryAfterS": math.ceil(exc.retry_after_s)},
    )


def _failure_error(exc: SpeechFailure) -> BotaiError:
    if exc.reason == "rate_limited":
        wait = math.ceil(exc.retry_after_s) if exc.retry_after_s else None
        detail = (
            f"Сервис распознавания перегружен. Повторите через {wait} с."
            if wait and wait <= 120
            else "Сервис распознавания перегружен. Повторите чуть позже или напишите текстом."
        )
        return BotaiError(
            ErrorCode.STT_RATE_LIMITED, detail, extra={"retryAfterS": wait} if wait else None
        )
    if exc.reason == "rejected":
        return BotaiError(ErrorCode.VALIDATION, "Не удалось разобрать запись. Запишите ещё раз.")
    return BotaiError(
        ErrorCode.STT_UNAVAILABLE,
        "Распознавание речи сейчас недоступно. Напишите текстом.",
    )
