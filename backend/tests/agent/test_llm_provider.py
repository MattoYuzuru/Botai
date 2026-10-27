"""DeepSeekProvider on a mocked transport (L0/L1): profiles, wire format, retries, quota."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from botai.agent.llm import (
    BudgetExceeded,
    DeepSeekProvider,
    MeteredLlm,
    is_peak,
    parse_profile,
    usage_scope,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import (
    LlmMessage,
    LlmRequest,
    LlmResponse,
    LlmTool,
    LlmToolCall,
    LlmUsage,
    ScriptedLlm,
)
from tests.agent.conftest import TEST_KEY, make_settings
from tests.agent.support import FakeGate, Wire, answer_chunks, provider, req, sse

# --- profiles -----------------------------------------------------------------------------------


def test_parse_profile() -> None:
    assert parse_profile("deepseek-flash:enabled:low").effort == "low"
    p = parse_profile("deepseek-flash:disabled:-")
    assert (p.thinking, p.effort) == ("disabled", None)
    for bad in ("deepseek-flash", "m:on:low", "m:enabled:medium", ":enabled:low"):
        with pytest.raises(ValueError, match="profile"):
            parse_profile(bad)


async def test_profile_by_purpose_is_applied() -> None:
    wire = Wire()
    await provider(wire).complete(req(purpose="chat"))
    body = wire.body()
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "low"
    assert "temperature" not in body  # ignored in thinking mode


async def test_classify_profile_disables_thinking() -> None:
    wire = Wire()
    await provider(wire).complete(req(purpose="classify", temperature=0.0))
    body = wire.body()
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body
    assert body["temperature"] == 0.0


async def test_request_fields_override_the_profile() -> None:
    wire = Wire()
    await provider(wire).complete(
        req(purpose="chat", model="deepseek-v4-pro", thinking="enabled", effort="max")
    )
    body = wire.body()
    assert (body["model"], body["reasoning_effort"]) == ("deepseek-v4-pro", "max")


async def test_profiles_come_from_settings_and_tier_pro_wins_over_profile_model() -> None:
    wire = Wire()
    p = provider(wire, llm_profile_learn="deepseek-v4-pro:enabled:max")
    await p.complete(req(purpose="learn"))
    assert wire.body()["model"] == "deepseek-v4-pro"
    assert wire.body()["reasoning_effort"] == "max"
    await p.complete(req(purpose="chat", tier="pro"))
    assert wire.body()["model"] == "deepseek-v4-pro"


async def test_bench_purpose_has_no_profile_and_uses_provider_defaults() -> None:
    wire = Wire()
    await provider(wire).complete(req(purpose="bench"))
    body = wire.body()
    assert "thinking" not in body
    assert "reasoning_effort" not in body


async def test_thinking_gets_headroom_in_max_tokens() -> None:
    wire = Wire()
    p = provider(wire)
    await p.complete(req(purpose="learn", max_tokens=2000))  # high: +12k
    assert wire.body()["max_tokens"] == 14_000
    await p.complete(req(purpose="classify", max_tokens=300))  # thinking disabled: as asked
    assert wire.body()["max_tokens"] == 300


# --- wire format --------------------------------------------------------------------------------


async def test_headers_and_url() -> None:
    wire = Wire()
    await provider(wire).complete(req())
    r = wire.requests[0]
    assert str(r.url) == "https://api.deepseek.com/chat/completions"
    assert r.headers["authorization"] == f"Bearer {TEST_KEY}"


async def test_reasoning_content_is_sent_back_with_tools_only() -> None:
    history = [
        LlmMessage(role="user", content="что завтра?"),
        LlmMessage(
            role="assistant",
            content="",
            reasoning_content="сначала посмотрю расписание",
            tool_calls=[LlmToolCall(id="c1", name="get_schedule", arguments={"day": "tomorrow"})],
        ),
        LlmMessage(role="tool", content="<source_data>...</source_data>", tool_call_id="c1"),
    ]
    tool = LlmTool(name="get_schedule", description="d", parameters={"type": "object"})
    wire = Wire()
    p = provider(wire)

    await p.complete(LlmRequest(messages=history, tools=[tool]))
    sent = wire.body()["messages"]
    assert sent[1]["reasoning_content"] == "сначала посмотрю расписание"
    assert sent[1]["tool_calls"][0]["function"]["arguments"] == '{"day": "tomorrow"}'
    assert sent[2]["tool_call_id"] == "c1"
    assert wire.body()["tools"][0]["function"]["name"] == "get_schedule"

    await p.complete(LlmRequest(messages=history))  # no tools in the request
    assert "reasoning_content" not in wire.body()["messages"][1]


async def test_json_schema_becomes_json_object_plus_instruction_after_system() -> None:
    wire = Wire(lambda _r: sse(*answer_chunks('{"a":1}')))
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    r = LlmRequest(
        messages=[
            LlmMessage(role="system", content="правила"),
            LlmMessage(role="user", content="дай объект"),
        ],
        json_schema=schema,
    )
    out = await provider(wire).complete(r)
    body = wire.body()
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "system", "user"]
    instruction = body["messages"][1]["content"]
    assert "JSON" in instruction
    assert '"integer"' in instruction
    assert out.json() == {"a": 1}


async def test_body_is_byte_identical_for_the_same_request() -> None:
    wire = Wire()
    p = provider(wire)
    await p.complete(req("один и тот же вопрос", purpose="chat"))
    await p.complete(req("один и тот же вопрос", purpose="chat"))
    assert wire.requests[0].content == wire.requests[1].content


# --- responses ----------------------------------------------------------------------------------


async def test_stream_is_assembled_with_usage_and_reasoning() -> None:
    wire = Wire(lambda _r: sse(*answer_chunks("Завтра две пары", reasoning="думаю")))
    out = await provider(wire).complete(req(purpose="chat"))
    assert out.content == "Завтра две пары"
    assert out.reasoning_content == "думаю"
    u = out.usage
    assert (u.input_cache_hit, u.input_cache_miss, u.output, u.reasoning) == (80, 20, 20, 12)
    assert u.model == "deepseek-flash"
    assert u.latency_ms >= 0


async def test_stream_tool_calls_are_joined_by_index() -> None:
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_deadlines", "arguments": '{"incl'},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'ude_done": true}'}}]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    ]
    out = await provider(Wire(lambda _r: sse(*chunks))).complete(req())
    assert out.tool_calls == [
        LlmToolCall(id="call_1", name="get_deadlines", arguments={"include_done": True})
    ]
    assert out.usage.input_cache_miss == 10
    assert out.usage.input_cache_hit == 0


async def test_invalid_tool_arguments_are_marked_not_raised() -> None:
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "x", "function": {"name": "t", "arguments": "{oops"}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    ]
    out = await provider(Wire(lambda _r: sse(*chunks))).complete(req())
    assert out.tool_calls[0].arguments == {"__invalid_arguments__": "{oops"}


async def test_non_streaming_mode() -> None:
    payload = {
        "choices": [
            {
                "message": {"content": "привет", "reasoning_content": "мысль"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "prompt_cache_hit_tokens": 4},
    }
    wire = Wire(lambda _r: httpx.Response(200, json=payload))
    out = await provider(wire, stream=False).complete(req())
    assert "stream" not in wire.body()
    assert (out.content, out.reasoning_content) == ("привет", "мысль")
    assert (out.usage.input_cache_hit, out.usage.input_cache_miss) == (4, 3)


async def test_length_with_nothing_but_reasoning_is_a_human_error() -> None:
    chunks = [{"choices": [{"delta": {"reasoning_content": "..."}, "finish_reason": "length"}]}]
    with pytest.raises(BotaiError) as exc:
        await provider(Wire(lambda _r: sse(*chunks))).complete(req())
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE
    assert "размышлен" in exc.value.detail


# --- retries and errors -------------------------------------------------------------------------


async def test_retries_5xx_twice_then_succeeds() -> None:
    sleeps: list[float] = []
    wire = Wire(
        lambda _r: httpx.Response(503),
        lambda _r: httpx.Response(500),
        lambda _r: sse(*answer_chunks()),
    )
    out = await provider(wire, sleeps=sleeps).complete(req())
    assert out.content == "ок"
    assert len(wire.requests) == 3
    assert len(sleeps) == 2


async def test_gives_up_after_two_retries_with_llm_unavailable() -> None:
    wire = Wire(lambda _r: httpx.Response(503))
    gate = FakeGate()
    with usage_scope("U1", "J1"), pytest.raises(BotaiError) as exc:
        await provider(wire, gate=gate).complete(req())
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE
    assert exc.value.status == 503
    assert len(wire.requests) == 3
    assert gate.recorded == []  # nothing is billed for a failed call
    assert TEST_KEY not in exc.value.detail
    assert "http" not in exc.value.detail.lower()


async def test_429_honours_retry_after_and_timeouts_are_retried() -> None:
    sleeps: list[float] = []

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    wire = Wire(
        lambda _r: httpx.Response(429, headers={"retry-after": "7"}),
        timeout,
        lambda _r: sse(*answer_chunks()),
    )
    out = await provider(wire, sleeps=sleeps).complete(req())
    assert out.content == "ок"
    assert sleeps[0] == 7.0
    assert sleeps[1] > 0


@pytest.mark.parametrize(
    ("status", "needle"), [(400, "не приняла"), (401, "Ключ"), (402, "средства")]
)
async def test_client_errors_are_not_retried(status: int, needle: str) -> None:
    wire = Wire(lambda _r: httpx.Response(status, json={"error": {"message": "bad"}}))
    with pytest.raises(BotaiError) as exc:
        await provider(wire).complete(req())
    assert len(wire.requests) == 1
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE
    assert needle in exc.value.detail


async def test_stream_without_finish_reason_is_retried() -> None:
    broken = [{"choices": [{"delta": {"content": "поло"}}]}]
    wire = Wire(lambda _r: sse(*broken), lambda _r: sse(*answer_chunks()))
    out = await provider(wire).complete(req())
    assert out.content == "ок"
    assert len(wire.requests) == 2


async def test_missing_key_is_a_human_error_without_a_request() -> None:
    wire = Wire()
    with pytest.raises(BotaiError) as exc:
        await provider(wire, deepseek_api_key="").complete(req())
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE
    assert wire.requests == []


async def test_redirects_are_not_followed() -> None:
    wire = Wire(lambda _r: httpx.Response(302, headers={"location": "https://evil.test/"}))
    with pytest.raises(BotaiError):
        await provider(wire).complete(req())
    assert {r.url.host for r in wire.requests} == {"api.deepseek.com"}


@pytest.mark.parametrize(
    "url", ["https://evil.test", "http://api.deepseek.com", "https://api.deepseek.com.evil.test"]
)
def test_only_the_deepseek_host_is_allowed(url: str) -> None:
    with pytest.raises(ValueError, match=r"api\.deepseek\.com"):
        DeepSeekProvider(make_settings(llm_base_url=url))


async def test_key_and_prompt_never_reach_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    wire = Wire(lambda _r: httpx.Response(503), lambda _r: sse(*answer_chunks("секретный ответ")))
    await provider(wire).complete(req("секретный вопрос про P@ssw0rd-FIXTURE"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert TEST_KEY not in text
    assert "секретный" not in text
    assert "P@ssw0rd-FIXTURE" not in text
    assert "llm call" in text  # but the accounting line is there


# --- quota and usage ----------------------------------------------------------------------------


async def test_quota_is_checked_before_the_request_and_blocks_it() -> None:
    wire = Wire()
    gate = FakeGate(deny=True)
    with usage_scope("U1", "J1"), pytest.raises(BotaiError) as exc:
        await provider(wire, gate=gate).complete(req())
    assert exc.value.code == ErrorCode.QUOTA_EXCEEDED
    assert gate.checked == ["U1"]
    assert wire.requests == []


async def test_usage_is_recorded_once_with_scope_and_purpose() -> None:
    gate = FakeGate()
    wire = Wire(lambda _r: httpx.Response(503), lambda _r: sse(*answer_chunks()))
    with usage_scope("U1", "J1"):
        await provider(wire, gate=gate).complete(req(purpose="repair"))
    assert gate.checked == ["U1"]
    (rec,) = gate.recorded
    assert (rec["user_id"], rec["job_id"], rec["purpose"]) == ("U1", "J1", "repair")
    assert rec["peak"] is False
    assert rec["usage"].input_cache_hit == 80


async def test_calls_outside_a_scope_are_not_accounted() -> None:
    gate = FakeGate()
    await provider(Wire(), gate=gate).complete(req())
    assert gate.checked == []
    assert gate.recorded == []


async def test_a_failing_recorder_does_not_lose_the_answer() -> None:
    class BrokenGate(FakeGate):
        async def record(self, **kw: Any) -> None:
            raise RuntimeError("db down")

    with usage_scope("U1"):
        out = await provider(Wire(), gate=BrokenGate()).complete(req())
    assert out.content == "ок"


def test_peak_hours() -> None:
    assert is_peak(datetime(2026, 10, 28, 7, 0, tzinfo=UTC))  # Wed 07:00 UTC
    assert is_peak(datetime(2026, 10, 28, 2, 59, tzinfo=UTC))
    assert not is_peak(datetime(2026, 10, 28, 4, 0, tzinfo=UTC))
    assert not is_peak(datetime(2026, 10, 31, 7, 0, tzinfo=UTC))  # Saturday


# --- metering -----------------------------------------------------------------------------------


async def test_metered_llm_sums_cost_and_stops_at_the_cap() -> None:
    usage = LlmUsage(input_cache_miss=1_000_000, model="deepseek-flash")  # $0.15 off-peak
    inner = ScriptedLlm(queue=[LlmResponse(content="a", usage=usage)] * 3)
    meter = MeteredLlm(inner, cap_usd=0.20, peak=False)
    await meter.complete(req())
    await meter.complete(req())  # crosses the cap, still returned
    assert meter.spent_usd == pytest.approx(0.30)
    with pytest.raises(BudgetExceeded):
        await meter.complete(req())
