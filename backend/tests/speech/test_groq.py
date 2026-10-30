"""The Groq provider against a replaced HTTP transport: what is sent, what is read, how it fails.

No test calls the real service. The key is a fixture value and must not appear in any error.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping

import httpx
import pytest

from botai.settings import Settings
from botai.speech.config import SpeechSettings
from botai.speech.groq import GroqSpeechToText
from botai.speech.provider import SpeechFailure

KEY = "gsk_test_FIXTURE_key_0123456789"
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 24

Handler = Callable[[httpx.Request], httpx.Response]


def speech(key: str = KEY, **over: object) -> SpeechSettings:
    return SpeechSettings(_env_file=None, groq_api_key=key, **over)  # type: ignore[call-arg,arg-type]


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

        return GroqSpeechToText(
            Settings(_env_file=None),  # type: ignore[call-arg]
            SpeechSettings(_env_file=None, groq_api_key=KEY, **over),  # type: ignore[call-arg,arg-type]
            transport=httpx.MockTransport(self),
            sleep=sleep,
        )


def ok(text: str = " Какие у меня пары завтра", **extra: object) -> httpx.Response:
    return httpx.Response(200, json={"text": text, "language": "russian", "duration": 2.4, **extra})


def fields(request: httpx.Request) -> dict[str, str]:
    """Text fields of the multipart body (the file is checked on its own)."""
    body = request.content.decode("latin-1")
    return dict(re.findall(r'name="(\w+)"\r\n\r\n([^\r]*)\r\n', body))


async def test_a_recording_is_posted_as_multipart_with_the_key_in_a_header() -> None:
    up = Upstream(ok())
    result = await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    assert result.text == "Какие у меня пары завтра"  # tidied: no leading space
    assert result.duration_s == 2.4  # what the service measured, for the quota
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


async def speak(*segments: Mapping[str, object], duration: float = 30.0, text: str = "x") -> str:
    up = Upstream(ok(text, segments=list(segments), duration=duration))
    result = await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    return result.text


async def test_a_segment_that_is_probably_silence_is_dropped_but_speech_stays() -> None:
    kept = await speak(
        {"text": " Привет", "no_speech_prob": 0.02, "avg_logprob": -0.2, "compression_ratio": 1.1},
        {"text": " Что-то из шума", "no_speech_prob": 0.92, "avg_logprob": -1.4},
        {"text": " ещё раз", "no_speech_prob": 0.7, "avg_logprob": -0.3},  # unsure but confident
    )
    assert kept == "Привет ещё раз"


async def test_repetition_alone_is_speech_it_is_dropped_only_together_with_silence() -> None:
    honest = {
        "text": " да, да, да, да, да, да",
        "no_speech_prob": 0.05,
        "avg_logprob": -0.4,
        "compression_ratio": 3.2,
    }
    loop = {
        "text": " ля ля ля ля ля ля",
        "no_speech_prob": 0.8,
        "avg_logprob": -0.5,
        "compression_ratio": 3.2,
    }
    assert await speak(honest, loop) == "да, да, да, да, да, да"


@pytest.mark.parametrize(
    "phrase",
    [
        "Продолжение следует...",
        "Субтитры сделал DimaTorzok",
        "Субтитры создавал DimaTorzok",
        "Редактор субтитров А.Семкин Корректор А.Егорова",
        "Спасибо за просмотр!",
        "Спасибо за просмотр",
    ],
)
async def test_a_stock_phrase_invented_from_silence_is_dropped_in_a_short_clip(phrase: str) -> None:
    segment = {"text": f" {phrase}", "no_speech_prob": 0.05, "avg_logprob": -0.6}
    assert await speak(segment, duration=3.2) == ""
    # In a long clip with clear speech the same words are believed.
    assert await speak(segment, duration=95.0) == phrase
    # A quiet segment is doubted even in a long clip.
    assert await speak({**segment, "no_speech_prob": 0.4}, duration=95.0) == ""


async def test_the_same_words_inside_a_real_sentence_are_not_a_stock_phrase() -> None:
    segment = {"text": " Спасибо за просмотр лекции, а теперь вопросы", "no_speech_prob": 0.01}
    assert await speak(segment, duration=4.0) == "Спасибо за просмотр лекции, а теперь вопросы"


async def test_a_stock_phrase_is_dropped_from_the_plain_text_of_a_short_clip_too() -> None:
    up = Upstream(ok("Продолжение следует...", duration=2.0))
    result = await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    assert (result.text, result.duration_s) == ("", 2.0)


async def test_nothing_said_is_an_empty_text_not_an_error() -> None:
    silence = [{"text": " Субтитры", "no_speech_prob": 0.95, "avg_logprob": -1.8}]
    up = Upstream(ok("Субтитры", segments=silence))
    assert (await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")).text == ""
    up = Upstream(ok("   ", segments=[]))
    assert (await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")).text == ""


async def test_a_missing_or_nonsense_duration_is_not_a_number_to_charge_by() -> None:
    for duration in (None, "long", -3, True, float("nan")):
        payload = {"text": "привет"}
        if duration is not None:
            payload["duration"] = duration  # type: ignore[assignment]
        up = Upstream(httpx.Response(200, content=json.dumps(payload).encode()))
        result = await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
        assert (result.text, result.duration_s) == ("привет", None)


async def test_a_server_error_is_retried_once() -> None:
    up = Upstream(httpx.Response(503, text="overloaded"), ok())
    assert (await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")).text != ""
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
        Settings(_env_file=None),  # type: ignore[call-arg]
        speech(key="  "),
        transport=httpx.MockTransport(up),
    )
    with pytest.raises(SpeechFailure) as failure:
        await provider.transcribe(WEBM, mime="audio/webm", language="ru")
    assert failure.value.reason == "misconfigured"
    assert failure.value.refundable  # nothing was sent: nothing was spent
    assert up.requests == []


@pytest.mark.parametrize(
    ("answer", "refundable"),
    [
        (httpx.Response(400), True),
        (httpx.Response(413), True),
        (httpx.Response(422), True),
        (httpx.Response(401), True),
        (httpx.Response(403), True),
        (httpx.Response(429, headers={"retry-after": "5"}), True),
        # The provider may have processed these: the seconds are not given back.
        (httpx.Response(500), False),
        (httpx.Response(502), False),
        (httpx.Response(503), False),
        (httpx.Response(408), False),
        (httpx.Response(307, headers={"location": "https://elsewhere.example/"}), False),
        (httpx.ReadTimeout("slow"), False),
        (httpx.ConnectError("refused"), False),
        (httpx.Response(200, text="<html>gateway</html>"), False),
    ],
)
async def test_only_a_plain_refusal_of_the_provider_is_refundable(
    answer: httpx.Response | Exception, refundable: bool
) -> None:
    up = Upstream(answer)
    with pytest.raises(SpeechFailure) as failure:
        await up.provider().transcribe(WEBM, mime="audio/webm", language="ru")
    assert failure.value.refundable is refundable
