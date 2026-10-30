"""Groq Speech-to-Text (OpenAI-compatible `audio/transcriptions`, Whisper models).

The request goes through the EgressProxy with a grant for one fixed host, like public search. The
key is a bearer header of that single request: it is never logged, never put in an error, and a
redirect is not followed with it. Nothing but the recording leaves: no id, name or chat.

`verbose_json` carries a probability of silence and a log-probability per segment, and the length
of the audio the service measured (the quota is charged by it). Whisper is known to invent a phrase
from silence or noise, so a segment is dropped when it is probably silence (no_speech > 0.6) and
either low-confidence (avg_logprob < -1) or repetitive (compression ratio > 2.4): the repetition
alone is not enough, a student may well say "да, да, да". The stock phrases it invents in Russian
("Продолжение следует…", "Субтитры сделал …", "Спасибо за просмотр") are dropped in a short clip or
a quiet segment.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from botai.browser.egress import proxy_url_for
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError
from botai.core.http import build_client, request_once_capped
from botai.settings import Settings
from botai.speech.audio import EXTENSIONS
from botai.speech.config import SpeechSettings
from botai.speech.provider import FailureReason, SpeechFailure, Transcription

HOST = "api.groq.com"
URL = f"https://{HOST}/openai/v1/audio/transcriptions"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ATTEMPTS = 2  # one retry
ATTEMPT_TIMEOUT_S = 30.0
RETRY_DELAY_S = 0.5
MAX_RETRY_AFTER_S = 3600.0

# Whisper's own thresholds for "this segment is silence, not speech".
NO_SPEECH_PROB = 0.6
LOW_LOGPROB = -1.0
REPETITIVE_RATIO = 2.4
# A stock phrase is believed in a long clip with clear speech and doubted in a short or quiet one.
SHORT_CLIP_S = 10.0
QUIET_SEGMENT = 0.2
_STOCK_PHRASE = re.compile(
    r"^(?:продолжение следует|спасибо за (?:просмотр|внимание)|подписывайтесь на (?:мой )?канал"
    r"|(?:субтитры|субтитр)\s+(?:сделал|создавал|подготовил|добавил)\w*(?:\s.*)?"
    r"|редактор субтитров(?:\s.*)?|корректор(?:\s.*)?)$"
)

Sleep = Callable[[float], Awaitable[None]]


class GroqSpeechToText:
    name = "groq"

    def __init__(
        self,
        settings: Settings,
        speech: SpeechSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings  # the egress proxy
        self.speech = speech
        self._transport = transport  # tests only: no proxy, no network
        self._sleep = sleep
        self._now = monotonic

    def _key(self) -> str:
        return self.speech.groq_api_key.get_secret_value().strip()

    async def transcribe(self, audio: bytes, *, mime: str, language: str | None) -> Transcription:
        if not self._key():
            raise SpeechFailure("misconfigured", refundable=True)  # nothing was sent
        deadline = self._now() + self.speech.stt_deadline_s
        failure: SpeechFailure | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - self._now()
            if remaining <= 0:
                break
            try:
                return await self._once(audio, mime, language, min(ATTEMPT_TIMEOUT_S, remaining))
            except SpeechFailure as exc:
                failure = exc
            diagnostic_event(
                "speech.failed",
                provider="groq",
                reason=_REASON_CODE[failure.reason],
                status=failure.status or 0,
                attempt=attempt,
            )
            if failure.reason != "unavailable" or attempt == MAX_ATTEMPTS:
                break
            if deadline - self._now() <= RETRY_DELAY_S:
                break
            diagnostic_event("speech.retry", provider="groq", delay_ms=int(RETRY_DELAY_S * 1000))
            await self._sleep(RETRY_DELAY_S)
        raise failure or SpeechFailure("unavailable")

    async def _once(
        self, audio: bytes, mime: str, language: str | None, budget_s: float
    ) -> Transcription:
        try:
            response = await self._send(audio, mime, language, budget_s)
        except (TimeoutError, httpx.TransportError, BotaiError) as exc:
            raise SpeechFailure("unavailable") from exc
        failure = _failure_of(response)
        if failure is not None:
            raise failure
        try:
            return _text_of(json.loads(response.content))
        except (ValueError, TypeError) as exc:
            raise SpeechFailure("unavailable", status=response.status_code) from exc

    async def _send(
        self, audio: bytes, mime: str, language: str | None, budget_s: float
    ) -> httpx.Response:
        proxy = (
            None
            if self._transport is not None
            else await proxy_url_for(self.settings, [HOST], ttl_s=max(60.0, budget_s + 10))
        )
        data = {
            "model": self.speech.stt_model,
            "temperature": "0",
            "response_format": "verbose_json",
        }
        if language:
            data["language"] = language
        files = {"file": (f"recording.{EXTENSIONS.get(mime, 'webm')}", audio, mime)}
        async with build_client(
            allow_hosts=[HOST],
            proxy_url=proxy,
            headers={"Authorization": f"Bearer {self._key()}"},
            transport=self._transport,
        ) as client:
            return await request_once_capped(
                client,
                "POST",
                URL,
                budget_s=budget_s,
                max_bytes=MAX_RESPONSE_BYTES,
                follow_redirects=False,  # the key must not follow a redirect to another host
                data=data,
                files=files,
            )


# The closed vocabulary of diagnostics (core/diagnostics.py) has no free text.
_REASON_CODE: dict[FailureReason, str] = {
    "unavailable": "unavailable",
    "rate_limited": "rate_limited",
    "rejected": "validation",
    "misconfigured": "invalid_config",
}


def _failure_of(response: httpx.Response) -> SpeechFailure | None:
    status = response.status_code
    if status == 200:
        return None
    plain_refusal = 400 <= status < 500 and status not in {408, 425}
    if status == 429:
        return SpeechFailure(
            "rate_limited", status=status, retry_after_s=_retry_after(response), refundable=True
        )
    if status in {408, 425} or status >= 500:
        return SpeechFailure("unavailable", status=status)
    if status in {400, 413, 415, 422}:
        return SpeechFailure("rejected", status=status, refundable=True)
    # 401/402/403/404 and redirects: a wrong or closed key, a removed model, an access policy.
    return SpeechFailure("misconfigured", status=status, refundable=plain_refusal)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return min(MAX_RETRY_AFTER_S, max(0.0, seconds))


def _number(value: Any, default: float) -> float:
    return (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    )


def _is_silence(segment: dict[str, Any]) -> bool:
    return _number(segment.get("no_speech_prob"), 0.0) > NO_SPEECH_PROB and (
        _number(segment.get("avg_logprob"), 0.0) < LOW_LOGPROB
        or _number(segment.get("compression_ratio"), 0.0) > REPETITIVE_RATIO
    )


def tidy(text: str) -> str:
    """One line of words: the model's leading space, line breaks and doubled spaces are removed."""
    return re.sub(r"\s+", " ", text).strip()


def _is_stock_phrase(text: str, *, clip_s: float | None, no_speech: float) -> bool:
    """A phrase Whisper invents from silence, in a clip where nobody could have said it."""
    words = re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е"))
    if not _STOCK_PHRASE.fullmatch(tidy(words)):
        return False
    return (clip_s is not None and clip_s < SHORT_CLIP_S) or no_speech > QUIET_SEGMENT


def _text_of(payload: Any) -> Transcription:
    if not isinstance(payload, dict):
        raise ValueError("invalid transcription")
    measured = payload.get("duration")
    clip_s = (
        float(measured)
        if isinstance(measured, (int, float))
        and not isinstance(measured, bool)
        and math.isfinite(measured)
        and measured >= 0
        else None
    )
    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        kept = [
            seg["text"]
            for seg in segments
            if isinstance(seg, dict)
            and isinstance(seg.get("text"), str)
            and not _is_silence(seg)
            and not _is_stock_phrase(
                seg["text"], clip_s=clip_s, no_speech=_number(seg.get("no_speech_prob"), 0.0)
            )
        ]
        return Transcription(tidy(" ".join(kept)), clip_s)
    text = payload.get("text")
    if not isinstance(text, str):
        raise ValueError("invalid transcription")
    if _is_stock_phrase(text, clip_s=clip_s, no_speech=0.0):
        return Transcription("", clip_s)
    return Transcription(tidy(text), clip_s)
