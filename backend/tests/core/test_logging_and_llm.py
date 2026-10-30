from __future__ import annotations

import pytest

from botai.core.llm import LlmFixtureMissing, LlmMessage, LlmRequest, LlmResponse, ScriptedLlm
from botai.core.logging import REDACTED, redact


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
