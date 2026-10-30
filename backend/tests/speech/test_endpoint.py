"""POST /api/v1/speech/transcribe: shape of the recording, limits, quota, provider failures and
what must never leave the endpoint (the key, the audio, the words)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Generator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from botai.attachments.limits import BODY_LIMIT
from botai.core import diagnostics
from botai.core.clock import FrozenClock
from botai.db import models as m
from botai.settings import Settings
from botai.speech.groq import GroqSpeechToText
from botai.speech.provider import FailureReason, SpeechFailure
from botai.speech.quota import SpeechQuota
from botai.speech.router import get_speech
from botai.speech.service import MAX_AUDIO_BYTES, SpeechService, build_provider
from tests.auth.support import Sm, signup

URL = "/api/v1/speech/transcribe"
KEY = "gsk_test_FIXTURE_key_0123456789"
WEBM = b"\x1a\x45\xdf\xa3" + b"\x93\x42\x82" * 400  # 1.2 kB of "audio"
WORDS = "Какие у меня пары завтра"
Make = Callable[[], httpx.AsyncClient]


class Fake:
    """A provider that remembers what it was given and answers from a script."""

    name = "groq"

    def __init__(self, answer: str | SpeechFailure = WORDS) -> None:
        self.answer = answer
        self.calls: list[tuple[bytes, str, str | None]] = []

    async def transcribe(self, audio: bytes, *, mime: str, language: str | None) -> str:
        self.calls.append((audio, mime, language))
        if isinstance(self.answer, SpeechFailure):
            raise self.answer
        return self.answer


class Wall:
    def __init__(self) -> None:
        self.now = 2_000_000.0

    def __call__(self) -> float:
        return self.now


def install(
    app: FastAPI, settings: Settings, provider: Fake | None, wall: Wall | None = None
) -> None:
    quota = SpeechQuota(
        per_hour=settings.stt_requests_per_hour,
        seconds_per_day=settings.stt_seconds_per_day,
        clock=wall or Wall(),
    )
    service = SpeechService(settings, provider=provider, quota=quota)
    app.dependency_overrides[get_speech] = lambda: service


async def post(
    client: httpx.AsyncClient,
    data: bytes = WEBM,
    *,
    name: str = "dictation.webm",
    content_type: str = "audio/webm",
    **form: str,
) -> httpx.Response:
    return await client.post(URL, files={"audio": (name, data, content_type)}, data=form)


@pytest.fixture
async def ready(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    settings: Settings,
) -> Fake:
    await signup(client, tx_sessionmaker, clock)
    provider = Fake()
    install(app, settings, provider)
    return provider


async def test_a_recording_becomes_text_in_russian_by_default(
    client: httpx.AsyncClient, ready: Fake
) -> None:
    r = await post(client)
    assert r.status_code == 200
    assert r.json() == {"text": WORDS}
    assert ready.calls == [(WEBM, "audio/webm", "ru")]


async def test_the_container_is_read_from_the_bytes_not_from_the_declared_type(
    client: httpx.AsyncClient, ready: Fake
) -> None:
    ogg = b"OggS\x00\x02" + b"\x00" * 64
    r = await post(client, ogg, name="a.mp3", content_type="audio/mpeg")
    assert r.status_code == 200
    assert ready.calls[0][1] == "audio/ogg"


async def test_an_explicit_language_is_passed_on(client: httpx.AsyncClient, ready: Fake) -> None:
    assert (await post(client, language="en")).status_code == 200
    assert ready.calls[0][2] == "en"


@pytest.mark.parametrize("language", ["russian", "RU", "r", "ру", "1x"])
async def test_a_malformed_language_is_refused_before_the_provider(
    client: httpx.AsyncClient, ready: Fake, language: str
) -> None:
    r = await post(client, language=language)
    assert (r.status_code, r.json()["code"]) == (422, "validation")
    assert ready.calls == []


async def test_it_needs_a_signed_in_user(
    app: FastAPI, client: httpx.AsyncClient, settings: Settings
) -> None:
    provider = Fake()
    install(app, settings, provider)
    r = await post(client)
    assert (r.status_code, r.json()["code"]) == (401, "unauthorized")
    assert provider.calls == []


# --- the shape of the recording --------------------------------------------------------------


async def test_an_empty_recording_and_a_non_audio_file_are_refused(
    client: httpx.AsyncClient, ready: Fake
) -> None:
    empty = await post(client, b"")
    assert (empty.status_code, empty.json()["code"]) == (422, "validation")
    png = await post(client, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, content_type="audio/webm")
    assert (png.status_code, png.json()["code"]) == (422, "validation")
    assert ready.calls == []


async def test_a_recording_over_20_mib_is_refused_with_413(
    client: httpx.AsyncClient, ready: Fake
) -> None:
    r = await post(client, WEBM + b"\x00" * (MAX_AUDIO_BYTES - len(WEBM) + 1))
    assert (r.status_code, r.json()["code"]) == (413, "validation")
    assert "20 МБ" in r.json()["detail"]
    assert ready.calls == []


async def test_a_body_far_over_the_limit_is_cut_off_before_it_is_read(
    client: httpx.AsyncClient, ready: Fake
) -> None:
    r = await post(client, WEBM + b"\x00" * (BODY_LIMIT + 1024))
    assert r.status_code == 413
    assert r.headers["connection"] == "close"
    assert ready.calls == []


async def test_a_claimed_length_over_three_minutes_is_refused(
    client: httpx.AsyncClient, ready: Fake
) -> None:
    assert (
        await post(client, durationMs="185000")
    ).status_code == 200  # the page's clock is not exact
    r = await post(client, durationMs="200000")
    assert (r.status_code, r.json()["code"]) == (413, "validation")
    assert "трёх минут" in r.json()["detail"]
    assert len(ready.calls) == 1


# --- off, and the provider's failures --------------------------------------------------------


async def test_a_service_that_is_off_answers_503_with_its_own_code_and_reads_nothing(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    settings: Settings,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    install(app, settings, None)
    r = await post(client)
    assert (r.status_code, r.json()["code"]) == (503, "stt_unavailable")
    assert r.headers["content-type"] == "application/problem+json"
    assert "текстом" in r.json()["detail"]


def test_dictation_is_off_without_a_key_or_by_switch() -> None:
    def build(**over: object) -> object:
        return build_provider(Settings(_env_file=None, **over))  # type: ignore[call-arg,arg-type]

    assert build() is None  # the default provider is groq, but there is no key
    assert build(groq_api_key=" ") is None
    assert build(groq_api_key=KEY, stt_provider="off") is None
    assert isinstance(build(groq_api_key=KEY), GroqSpeechToText)


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [
        ("unavailable", 503, "stt_unavailable"),
        ("misconfigured", 503, "stt_unavailable"),
        ("rate_limited", 429, "stt_rate_limited"),
        ("rejected", 422, "validation"),
    ],
)
async def test_provider_failures_are_told_in_words_with_a_code_of_our_own(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    settings: Settings,
    reason: FailureReason,
    status: int,
    code: str,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    install(app, settings, Fake(SpeechFailure(reason, status=500, retry_after_s=17)))
    r = await post(client)
    assert (r.status_code, r.json()["code"]) == (status, code)
    detail = r.json()["detail"]
    assert detail
    assert "groq" not in detail.lower()
    assert "HTTP" not in detail
    if reason == "rate_limited":
        assert "17 с" in detail
        assert r.json()["retryAfterS"] == 17


async def test_a_nothing_said_recording_is_an_empty_text(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    settings: Settings,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    install(app, settings, Fake("  "))
    r = await post(client)
    assert (r.status_code, r.json()) == (200, {"text": ""})


# --- the user's quota ------------------------------------------------------------------------


async def test_requests_per_hour_are_limited_per_user_and_come_back_with_the_hour(
    app: FastAPI,
    new_client: Make,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    settings: Settings,
) -> None:
    settings.stt_requests_per_hour = 2
    wall, provider = Wall(), Fake()
    install(app, settings, provider, wall)
    first, second = new_client(), new_client()
    await signup(first, tx_sessionmaker, clock)
    await signup(second, tx_sessionmaker, clock)
    assert [(await post(first)).status_code for _ in range(2)] == [200, 200]
    refused = await post(first)
    assert (refused.status_code, refused.json()["code"]) == (429, "quota_exceeded")
    assert refused.json()["scope"] == "speech_hour"
    assert "мин" in refused.json()["detail"]
    assert len(provider.calls) == 2  # the refused one never reached the provider
    assert (await post(second)).status_code == 200  # another student is not affected
    wall.now += 3601
    assert (await post(first)).status_code == 200


async def test_seconds_per_day_are_charged_by_the_claimed_length(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    settings: Settings,
) -> None:
    settings.stt_seconds_per_day = 300
    await signup(client, tx_sessionmaker, clock)
    install(app, settings, Fake(), Wall())
    assert [(await post(client, durationMs="100000")).status_code for _ in range(3)] == [200] * 3
    refused = await post(client, durationMs="1000")
    assert (refused.status_code, refused.json()["code"]) == (429, "quota_exceeded")
    assert refused.json()["scope"] == "speech_day"


async def test_a_recording_cannot_claim_to_be_shorter_than_its_size_allows(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    settings: Settings,
) -> None:
    settings.stt_seconds_per_day = 300
    await signup(client, tx_sessionmaker, clock)
    install(app, settings, Fake(), Wall())
    big = WEBM + b"\x00" * 8_000_000  # at 40 kB/s that is at least 200 s, whatever the claim
    assert (await post(client, big, durationMs="1000")).status_code == 200
    again = await post(client, big, durationMs="1000")
    assert (again.status_code, again.json()["scope"]) == (429, "speech_day")


async def test_a_provider_failure_does_not_spend_the_day(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    settings: Settings,
) -> None:
    settings.stt_seconds_per_day = 300
    await signup(client, tx_sessionmaker, clock)
    provider = Fake(SpeechFailure("unavailable"))
    install(app, settings, provider, Wall())
    for _ in range(3):
        assert (await post(client, durationMs="150000")).status_code == 503
    provider.answer = WORDS
    assert (await post(client, durationMs="150000")).status_code == 200


# --- what must not leave or stay -------------------------------------------------------------


async def test_no_response_header_or_body_carries_the_key(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    """The real Groq provider on a replaced transport that echoes the key in its own error."""
    await signup(client, tx_sessionmaker, clock)
    settings = Settings(_env_file=None, env="test", groq_api_key=KEY)  # type: ignore[call-arg,arg-type]

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}})

    provider = GroqSpeechToText(settings, transport=httpx.MockTransport(upstream))
    app.dependency_overrides[get_speech] = lambda: SpeechService(settings, provider=provider)
    r = await post(client)
    assert r.status_code == 503
    assert KEY not in r.text
    assert KEY not in json.dumps(dict(r.headers))
    app.dependency_overrides[get_speech] = lambda: SpeechService(
        settings,
        provider=GroqSpeechToText(
            settings,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"text": WORDS, "x_groq": {"id": KEY}})
            ),
        ),
    )
    ok = await post(client)
    assert ok.json() == {"text": WORDS}
    assert KEY not in ok.text


async def test_the_recording_is_not_stored_as_a_file_or_an_attachment(
    client: httpx.AsyncClient, ready: Fake, tx_sessionmaker: Sm, tmp_path: Path
) -> None:
    assert (await post(client)).status_code == 200
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(m.Attachment)) == 0
        assert await s.scalar(select(func.count()).select_from(m.Document)) == 0


@pytest.fixture
def traces(tmp_path: Path) -> Generator[Path]:
    diagnostics.configure_diagnostics(
        Settings(_env_file=None, botai_diagnostics=True, botai_diagnostics_dir=str(tmp_path))  # type: ignore[call-arg]
    )
    yield tmp_path
    diagnostics.configure_diagnostics(
        Settings(_env_file=None, botai_diagnostics=False, botai_diagnostics_dir=str(tmp_path))  # type: ignore[call-arg]
    )


async def test_diagnostics_keep_counters_but_neither_the_words_nor_the_audio_nor_the_key(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    traces: Path,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    settings = Settings(_env_file=None, env="test", groq_api_key=KEY)  # type: ignore[call-arg,arg-type]
    answers = [
        httpx.Response(503),
        httpx.Response(200, json={"text": WORDS}),
    ]
    provider = GroqSpeechToText(
        settings,
        transport=httpx.MockTransport(lambda _: answers.pop(0)),
        sleep=lambda _: _noop(),
    )
    app.dependency_overrides[get_speech] = lambda: SpeechService(settings, provider=provider)
    assert (await post(client)).json() == {"text": WORDS}
    text = await asyncio.to_thread(_read_traces, traces)
    records = [json.loads(line) for line in text.splitlines()]
    events = {r["event"] for r in records}
    assert {"speech.failed", "speech.retry", "speech.transcribed"} <= events
    done = next(r for r in records if r["event"] == "speech.transcribed")
    assert (done["provider"], done["outcome"], done["text_chars"]) == ("groq", "ok", len(WORDS))
    assert done["body_bytes"] == len(WEBM)
    for secret in (KEY, WORDS, "пары"):
        assert secret not in text


def _read_traces(directory: Path) -> str:
    return "\n".join(p.read_text() for p in directory.glob("*.jsonl*"))


async def _noop() -> None:
    return None
