from __future__ import annotations

import pytest

from botai.core.llm import LlmFixtureMissing, LlmMessage, LlmRequest, LlmResponse, ScriptedLlm
from botai.core.logging import REDACTED, configure_logging, redact


def test_redact_keys_and_tokens() -> None:
    out = redact(
        {
            "password": "x",
            "nested": {"Cookie": "a=b"},
            "msg": "Bearer abc.def.ghi",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        }
    )
    assert out["password"] == REDACTED
    assert out["nested"]["Cookie"] == REDACTED
    assert "abc.def.ghi" not in out["msg"]
    assert out["jwt"] == REDACTED


@pytest.mark.parametrize(
    "line",
    [
        'HTTP Request: GET https://calendar.example.org/export/ics.xml?private_token=k9Xq2LmZp7Vb&tz_id=Europe/Moscow "HTTP/1.1 200 OK"',
        "fetch https://calendar.example.org/ical/k9Xq2LmZp7Vb/basic.ics failed",
        "fetch webcal://cal.example.org/feed?x=k9Xq2LmZp7Vb failed",
        "fetch https://lms.example.org/page?x=1&sesskey=k9Xq2LmZp7Vb failed",
        "fetch https://lms.example.org/page?x=1&access_key=k9Xq2LmZp7Vb failed",
    ],
)
def test_a_secret_in_an_address_is_hidden_in_a_log_line(line: str) -> None:
    assert "k9Xq2LmZp7Vb" not in redact(line)
    assert "k9Xq2LmZp7Vb" not in redact({"msg": line}["msg"])


def test_ordinary_addresses_stay_readable_in_a_log_line() -> None:
    line = "fetch https://lms.example.org/week?begin_date=2026-10-26 done"
    assert redact(line) == line


def test_the_http_client_does_not_write_the_address_of_every_request() -> None:
    """httpx logs `GET <address with its query>` at INFO: a key in a link would reach the logs."""
    import logging

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    quiet = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        configure_logging()
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        for name, previous in quiet.items():
            logging.getLogger(name).setLevel(previous)


async def test_scripted_llm_replays_and_fails_loudly() -> None:
    req = LlmRequest(messages=[LlmMessage(role="user", content="привет")])
    llm = ScriptedLlm(by_fingerprint={req.fingerprint(): LlmResponse(content="ответ")})
    assert (await llm.complete(req)).content == "ответ"
    with pytest.raises(LlmFixtureMissing):
        await llm.complete(LlmRequest(messages=[LlmMessage(role="user", content="другое")]))


def test_fingerprint_ignores_purpose_but_not_effort() -> None:
    a = LlmRequest(messages=[LlmMessage(role="user", content="x")], purpose="agent")
    b = LlmRequest(messages=[LlmMessage(role="user", content="x")], purpose="learn")
    c = LlmRequest(messages=[LlmMessage(role="user", content="x")], effort="max")
    assert a.fingerprint() == b.fingerprint() != c.fingerprint()


async def test_discovery_script_never_consumes_an_author_reply() -> None:
    request = LlmRequest(messages=[LlmMessage(role="user", content="fixture")])
    author = LlmResponse(content="author")
    llm = ScriptedLlm(queue=[author])
    with pytest.raises(LlmFixtureMissing):
        await llm.complete_observation(request)
    assert not llm.requests
    assert not llm.observation_requests
    assert await llm.complete(request) == author
    scripted = ScriptedLlm(queue=[author], discovery_queue=[LlmResponse(content="plan")])
    assert (await scripted.complete_observation(request)).content == "plan"
    assert len(scripted.observation_requests) == 1
    assert not scripted.requests
