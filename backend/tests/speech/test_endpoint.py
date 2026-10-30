"""POST /api/v1/speech/transcribe: shape of the recording, limits, quota, provider failures and
what must never leave the endpoint (the key, the audio, the words)."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator, Callable, Generator
from pathlib import Path
from typing import IO, Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from botai.core import diagnostics
from botai.core.clock import FrozenClock
from botai.core.errors import BotaiError, ErrorCode
from botai.db import models as m
from botai.settings import Settings
from botai.speech.config import SpeechSettings
from botai.speech.groq import GroqSpeechToText
from botai.speech.limits import BODY_LIMIT, MAX_AUDIO_BYTES
from botai.speech.provider import FailureReason, SpeechFailure, Transcription
from botai.speech.quota import SpeechQuota
from botai.speech.router import get_speech
from botai.speech.service import SpeechService, build_provider
from tests.auth.support import Sm, signup

URL = "/api/v1/speech/transcribe"
KEY = "gsk_test_FIXTURE_key_0123456789"
WEBM = b"\x1a\x45\xdf\xa3" + b"\x93\x42\x82" * 400  # 1.2 kB of "audio"
WORDS = "Какие у меня пары завтра"
MIB = 1024 * 1024
Make = Callable[[], httpx.AsyncClient]


class Fake:
    """A provider that remembers what it was given and answers from a script.

    `heard` is the length the provider measures: it is what the quota is charged by."""

    name = "groq"

    def __init__(
        self,
        answer: str | Exception = WORDS,
        *,
        heard: float | None = None,
        hold: asyncio.Event | None = None,
    ) -> None:
        self.answer = answer
        self.heard = heard
        self.hold = hold
        self.calls: list[tuple[bytes, str, str | None]] = []

    async def transcribe(self, audio: bytes, *, mime: str, language: str | None) -> Transcription:
        self.calls.append((audio, mime, language))
        if self.hold is not None:
            await self.hold.wait()
        if isinstance(self.answer, Exception):
            raise self.answer
        return Transcription(self.answer, self.heard)


class Wall:
    def __init__(self) -> None:
        self.now = 2_000_000.0

    def __call__(self) -> float:
        return self.now


def install(
    app: FastAPI,
    provider: Fake | None,
    wall: Wall | None = None,
    *,
    per_hour: int = 60,
    per_day: int = 7200,
) -> SpeechService:
    quota = SpeechQuota(per_hour=per_hour, seconds_per_day=per_day, clock=wall or Wall())
    service = SpeechService(provider=provider, quota=quota)
    app.dependency_overrides[get_speech] = lambda: service
    return service


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
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> Fake:
    await signup(client, tx_sessionmaker, clock)
    provider = Fake()
    install(app, provider)
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


async def test_it_needs_a_signed_in_user(app: FastAPI, client: httpx.AsyncClient) -> None:
    provider = Fake()
    install(app, provider)
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


async def test_a_recording_over_5_mib_is_refused_with_413(
    client: httpx.AsyncClient, ready: Fake
) -> None:
    assert MAX_AUDIO_BYTES == 5 * MIB
    r = await post(client, WEBM + b"\x00" * (MAX_AUDIO_BYTES - len(WEBM) + 1))
    assert (r.status_code, r.json()["code"]) == (413, "validation")
    assert "5 МБ" in r.json()["detail"]
    assert ready.calls == []
    fits = await post(client, WEBM + b"\x00" * (MAX_AUDIO_BYTES - len(WEBM)))
    assert fits.status_code == 200


async def test_a_body_far_over_the_limit_is_refused_before_it_is_read(
    client: httpx.AsyncClient, ready: Fake
) -> None:
    r = await post(client, WEBM + b"\x00" * (BODY_LIMIT + 1024))
    assert r.status_code == 413
    assert r.headers["connection"] == "close"
    assert "5 МБ" in r.json()["detail"]  # the limit of this route, not the 20 MB of a file
    assert ready.calls == []


BOUNDARY = "botai-test-boundary"
CHUNKED = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}


async def _stream(total: int, seen: list[int]) -> AsyncIterator[bytes]:
    head = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="audio"; filename="a.webm"\r\n\r\n'
    )
    for piece in [head.encode()] + [b"\x00" * 65_536] * (total // 65_536):
        seen.append(len(piece))
        yield piece


async def test_a_streamed_body_without_a_length_is_cut_off_at_the_dictation_limit(
    client: httpx.AsyncClient, ready: Fake
) -> None:
    seen: list[int] = []
    r = await client.post(URL, content=_stream(20 * MIB, seen), headers=CHUNKED)
    assert r.status_code == 413
    assert sum(seen) <= BODY_LIMIT + 2 * 65_536  # it stopped reading soon after 6 MiB
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


async def test_a_service_that_is_off_answers_503_before_the_body_is_read(
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    install(app, None)
    seen: list[int] = []
    r = await client.post(URL, content=_stream(30 * MIB, seen), headers=CHUNKED)
    assert (r.status_code, r.json()["code"]) == (503, "stt_unavailable")
    assert r.headers["content-type"] == "application/problem+json"
    assert "текстом" in r.json()["detail"]
    assert sum(seen) <= 2 * 65_536  # a switched-off service does not take a recording first


def test_dictation_is_off_without_a_key_by_switch_or_with_wrong_settings() -> None:
    app_settings = Settings(_env_file=None)  # type: ignore[call-arg]

    def build(**over: object) -> object:
        speech = SpeechSettings(_env_file=None, **over)  # type: ignore[call-arg,arg-type]
        return build_provider(app_settings, speech)

    assert build() is None  # the default provider is groq, but there is no key
    assert build(groq_api_key=" ") is None
    assert build(groq_api_key=KEY, stt_provider="off") is None
    assert isinstance(build(groq_api_key=KEY), GroqSpeechToText)
    assert build_provider(app_settings, None) is None  # invalid STT_* settings


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
    reason: FailureReason,
    status: int,
    code: str,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    install(app, Fake(SpeechFailure(reason, status=500, retry_after_s=17)))
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
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    install(app, Fake("  "))
    r = await post(client)
    assert (r.status_code, r.json()) == (200, {"text": ""})


# --- the user's quota ------------------------------------------------------------------------


async def test_requests_per_hour_are_limited_per_user_and_come_back_with_the_hour(
    app: FastAPI, new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    wall, provider = Wall(), Fake()
    install(app, provider, wall, per_hour=2)
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


async def test_seconds_per_day_are_charged_by_the_claimed_length_until_the_provider_answers(
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    install(app, Fake(), Wall(), per_day=300)  # the provider does not say how long it was
    assert [(await post(client, durationMs="100000")).status_code for _ in range(3)] == [200] * 3
    refused = await post(client, durationMs="1000")
    assert (refused.status_code, refused.json()["code"]) == (429, "quota_exceeded")
    assert refused.json()["scope"] == "speech_day"


async def test_a_false_short_claim_does_not_help_the_provider_measured_length_is_charged(
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    """The client says one second; the provider heard two and a half minutes."""
    await signup(client, tx_sessionmaker, clock)
    install(app, Fake(heard=150.0), Wall(), per_day=300)
    assert (await post(client, durationMs="1000")).status_code == 200
    assert (await post(client, durationMs="1000")).status_code == 200
    again = await post(client, durationMs="1000")  # 300 s are spent, whatever was claimed
    assert (again.status_code, again.json()["scope"]) == (429, "speech_day")


async def test_an_honest_long_claim_is_charged_by_the_shorter_real_length(
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    install(app, Fake(heard=20.0), Wall(), per_day=300)
    for _ in range(10):  # claimed 100 s each, heard 20 s each: 200 s in all
        assert (await post(client, durationMs="100000")).status_code == 200


async def test_a_recording_longer_than_three_minutes_by_the_providers_measure_is_not_given_out(
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    provider = Fake(heard=400.0)
    install(app, provider, Wall(), per_day=600)
    r = await post(client, durationMs="1000")
    assert (r.status_code, r.json()["code"]) == (413, "validation")
    assert "трёх минут" in r.json()["detail"]
    assert WORDS not in r.text
    again = await post(client, durationMs="1000")  # the 400 s were charged: 200 s are left
    assert again.status_code == 413  # and the provider's measure is still over the line
    third = await post(client, durationMs="1000")
    assert (third.status_code, third.json()["scope"]) == (429, "speech_day")


async def test_a_recording_cannot_claim_to_be_shorter_than_its_size_allows(
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    install(app, Fake(), Wall(), per_day=300)
    big = WEBM + b"\x00" * 3_900_000  # at 40 kB/s that is at least 97 s, whatever the claim
    assert [(await post(client, big, durationMs="1000")).status_code for _ in range(3)] == [200] * 3
    again = await post(client, big, durationMs="1000")
    assert (again.status_code, again.json()["scope"]) == (429, "speech_day")


@pytest.mark.parametrize(
    "refusal",
    [
        SpeechFailure("rejected", status=400, refundable=True),
        SpeechFailure("rate_limited", status=429, refundable=True),
        SpeechFailure("misconfigured", status=401, refundable=True),
    ],
    ids=["400", "429", "401"],
)
async def test_a_plain_refusal_of_the_provider_does_not_spend_the_day(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    refusal: SpeechFailure,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    provider = Fake(refusal)
    install(app, provider, Wall(), per_day=300)
    for _ in range(3):
        assert (await post(client, durationMs="150000")).status_code in {422, 429, 503}
    provider.answer = WORDS
    assert (await post(client, durationMs="150000")).status_code == 200


@pytest.mark.parametrize(
    "failure",
    [
        SpeechFailure("unavailable"),  # a timeout or a dropped connection
        SpeechFailure("unavailable", status=503),
    ],
    ids=["timeout", "503"],
)
async def test_a_timeout_or_a_server_error_may_have_been_processed_and_stays_spent(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    failure: SpeechFailure,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    provider = Fake(failure)
    install(app, provider, Wall(), per_day=300)
    assert (await post(client, durationMs="150000")).status_code == 503
    assert (await post(client, durationMs="150000")).status_code == 503
    provider.answer = WORDS
    third = await post(client, durationMs="150000")  # 300 s are spent by the two failures
    assert (third.status_code, third.json()["scope"]) == (429, "speech_day")


async def test_a_cancelled_request_stays_spent(app: FastAPI) -> None:
    hold = asyncio.Event()
    service = install(app, Fake(hold=hold), Wall(), per_day=300)
    task = asyncio.create_task(service.transcribe("u1", WEBM, duration_ms=180_000))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    hold.set()
    with pytest.raises(BotaiError) as refused:  # 180 s are spent: another 150 s do not fit
        await service.transcribe("u1", WEBM, duration_ms=150_000)
    assert refused.value.code == ErrorCode.QUOTA_EXCEEDED


# --- what must not leave or stay -------------------------------------------------------------


async def test_no_response_header_or_body_carries_the_key(
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    """The real Groq provider on a replaced transport that echoes the key in its own error."""
    await signup(client, tx_sessionmaker, clock)
    app_settings = Settings(_env_file=None, env="test")  # type: ignore[call-arg]
    speech = SpeechSettings(_env_file=None, groq_api_key=KEY)  # type: ignore[call-arg,arg-type]

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}})

    def serve(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        provider = GroqSpeechToText(app_settings, speech, transport=httpx.MockTransport(handler))
        service = SpeechService(
            provider=provider, quota=SpeechQuota(per_hour=60, seconds_per_day=7200)
        )
        app.dependency_overrides[get_speech] = lambda: service

    serve(upstream)
    r = await post(client)
    assert r.status_code == 503
    assert KEY not in r.text
    assert KEY not in json.dumps(dict(r.headers))
    serve(lambda _: httpx.Response(200, json={"text": WORDS, "x_groq": {"id": KEY}}))
    ok = await post(client)
    assert ok.json() == {"text": WORDS}
    assert KEY not in ok.text


async def test_the_recording_is_not_kept_in_the_database(
    client: httpx.AsyncClient, ready: Fake, tx_sessionmaker: Sm
) -> None:
    assert (await post(client)).status_code == 200
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(m.Attachment)) == 0
        assert await s.scalar(select(func.count()).select_from(m.Document)) == 0


async def test_a_large_recording_is_spooled_to_the_temporary_directory_and_closed_with_the_request(
    app: FastAPI,
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above a megabyte the framework writes the upload to an anonymous file in the temporary
    directory (a tmpfs of the api container, compose.prod.yaml) and closes it with the request."""
    await signup(client, tx_sessionmaker, clock)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    real = tempfile.TemporaryFile
    spools: list[tuple[str, IO[bytes]]] = []

    def spy(*args: Any, **kwargs: Any) -> IO[bytes]:
        file = real(*args, **kwargs)
        spools.append((tempfile.gettempdir(), file))
        return file

    monkeypatch.setattr(tempfile, "TemporaryFile", spy)
    install(app, Fake())
    r = await post(client, WEBM + b"\x00" * (3 * MIB))
    assert r.status_code == 200
    assert [where for where, _ in spools] == [str(tmp_path)]
    assert all(file.closed for _, file in spools)


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
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, traces: Path
) -> None:
    await signup(client, tx_sessionmaker, clock)
    app_settings = Settings(_env_file=None, env="test")  # type: ignore[call-arg]
    speech = SpeechSettings(_env_file=None, groq_api_key=KEY)  # type: ignore[call-arg,arg-type]
    answers = [httpx.Response(503), httpx.Response(200, json={"text": WORDS, "duration": 2.0})]
    provider = GroqSpeechToText(
        app_settings,
        speech,
        transport=httpx.MockTransport(lambda _: answers.pop(0)),
        sleep=lambda _: _noop(),
    )
    service = SpeechService(provider=provider, quota=SpeechQuota(per_hour=60, seconds_per_day=7200))
    app.dependency_overrides[get_speech] = lambda: service
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
