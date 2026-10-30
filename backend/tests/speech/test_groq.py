"""The Groq provider against a replaced HTTP transport: what is sent, what is read, how it fails.

No test calls the real service. The key is a fixture value and must not appear in any error.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx
import pytest

from botai.settings import Settings
from botai.speech.groq import GroqSpeechToText
from botai.speech.provider import SpeechFailure

KEY = "gsk_test_FIXTURE_key_0123456789"
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 24

Handler = Callable[[httpx.Request], httpx.Response]


def settings(**over: object) -> Settings:
    return Settings(_env_file=None, groq_api_key=KEY, **over)  # type: ignore[call-arg,arg-type]


class Upstream:
    """A transport that answers from a script and remembers every request."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def provider(self, **over: object) -> GroqSpeechToText:
        self.sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        return GroqSpeechToText(settings(**over), transport=httpx.MockTransport(self), sleep=sleep)


def ok(text: str = " Какие у меня пары завтра", **extra: object) -> httpx.Response:
    return httpx.Response(200, json={"text": text, "language": "russian", **extra})


def fields(request: httpx.Request) -> dict[str, str]:
    """Text fields of the multipart body (the file is checked on its own)."""
    body = request.content.decode("latin-1")
    return dict(re.findall(r'name="(\w+)"\r\n\r\n([^\r]*)\r\n', body))


async def test_a_recording_is_posted_as_multipart_with_the_key_in_a_header() -> None:
    up = Upstream(ok())
    text = await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    assert text == "Какие у меня пары завтра"  # tidied: no leading space
    (request,) = up.requests
    assert (request.method, str(request.url)) == (
        "POST",
        "https://api.groq.com/openai/v1/audio/transcriptions",
    )
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert fields(request) == {
        "model": "whisper-large-v3-turbo",
        "temperature": "0",
        "response_format": "verbose_json",
        "language": "ru",
    }
    body = request.content
    assert b'name="file"; filename="recording.webm"' in body
    assert b"Content-Type: audio/webm" in body
    assert WEBM in body
    assert KEY.encode() not in body  # the key travels in the header only


async def test_the_model_comes_from_settings_and_a_missing_language_lets_the_service_detect() -> (
    None
):
    up = Upstream(ok())
    await up.provider(stt_model="whisper-large-v3").transcribe(
        b"OggS" + b"\x00" * 8, mime="audio/ogg", language=None
    )
    (request,) = up.requests
    sent = fields(request)
    assert sent["model"] == "whisper-large-v3"
    assert "language" not in sent
    assert b'filename="recording.ogg"' in request.content


async def test_a_segment_that_is_probably_silence_is_dropped_but_speech_stays() -> None:
    segments = [
        {"text": " Продолжение следует...", "no_speech_prob": 0.92, "avg_logprob": -1.4},
        {"text": " Привет", "no_speech_prob": 0.02, "avg_logprob": -0.2, "compression_ratio": 1.1},
        {
            "text": " да да да да да да",
            "no_speech_prob": 0.1,
            "avg_logprob": -0.5,
            "compression_ratio": 3.0,
        },
        {"text": " ещё раз", "no_speech_prob": 0.7, "avg_logprob": -0.3},  # unsure but confident
    ]
    up = Upstream(ok("ignored when segments are present", segments=segments))
    assert (
        await up.provider().transcribe(WEBM, mime="audio/webm", language="ru") == "Привет ещё раз"
    )


async def test_nothing_said_is_an_empty_text_not_an_error() -> None:
    silence = [{"text": " Субтитры", "no_speech_prob": 0.95, "avg_logprob": -1.8}]
    up = Upstream(ok("Субтитры", segments=silence))
    assert await up.provider().transcribe(WEBM, mime="audio/webm", language="ru") == ""
    up = Upstream(ok("   ", segments=[]))
    assert await up.provider().transcribe(WEBM, mime="audio/webm", language="ru") == ""


async def test_a_server_error_is_retried_once() -> None:
    up = Upstream(httpx.Response(503, text="overloaded"), ok())
    assert await up.provider().transcribe(WEBM, mime="audio/webm", language="ru") != ""
    assert len(up.requests) == 2
    assert up.sleeps == [0.5]


async def test_two_server_errors_in_a_row_give_up_as_unavailable() -> None:
    up = Upstream(httpx.Response(502), httpx.Response(500))
    with pytest.raises(SpeechFailure) as failure:
        await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    assert failure.value.reason == "unavailable"
    assert len(up.requests) == 2


async def test_a_dropped_connection_and_a_timeout_are_retried_then_unavailable() -> None:
    up = Upstream(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"))
    with pytest.raises(SpeechFailure) as failure:
        await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    assert failure.value.reason == "unavailable"
    assert len(up.requests) == 2


async def test_a_garbled_answer_is_a_failure_of_the_service_not_a_crash() -> None:
    up = Upstream(httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(SpeechFailure) as failure:
        await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    assert failure.value.reason == "unavailable"
    up = Upstream(httpx.Response(200, json={"no": "text"}))
    with pytest.raises(SpeechFailure):
        await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")


async def test_a_rate_limit_is_not_retried_and_carries_the_wait() -> None:
    up = Upstream(httpx.Response(429, headers={"retry-after": "17"}, json={"error": {}}))
    with pytest.raises(SpeechFailure) as failure:
        await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    assert (failure.value.reason, failure.value.retry_after_s, failure.value.status) == (
        "rate_limited",
        17.0,
        429,
    )
    assert len(up.requests) == 1


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, "rejected"),
        (413, "rejected"),
        (422, "rejected"),
        (401, "misconfigured"),
        (402, "misconfigured"),
        (403, "misconfigured"),
        (404, "misconfigured"),
    ],
)
async def test_client_errors_are_not_retried(status: int, reason: str) -> None:
    up = Upstream(httpx.Response(status, json={"error": {"message": f"echo {KEY}"}}))
    with pytest.raises(SpeechFailure) as failure:
        await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    assert failure.value.reason == reason
    assert len(up.requests) == 1
    assert KEY not in repr(failure.value) + str(failure.value)  # not even an echoing body


async def test_a_redirect_is_never_followed_with_the_key() -> None:
    up = Upstream(httpx.Response(307, headers={"location": "https://elsewhere.example/steal"}))
    with pytest.raises(SpeechFailure) as failure:
        await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    assert failure.value.reason == "misconfigured"
    assert [str(r.url.host) for r in up.requests] == ["api.groq.com"]


async def test_without_a_key_nothing_is_sent() -> None:
    up = Upstream(ok())
    provider = GroqSpeechToText(
        Settings(_env_file=None, groq_api_key="  "),  # type: ignore[call-arg,arg-type]
        transport=httpx.MockTransport(up),
    )
    with pytest.raises(SpeechFailure) as failure:
        await provider.transcribe(WEBM, mime="audio/webm", language="ru")
    assert failure.value.reason == "misconfigured"
    assert up.requests == []
