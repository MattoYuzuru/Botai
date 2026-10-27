"""L2: the real DeepSeek API. Opt-in and paid: BOTAI_LIVE_LLM=1 BOTAI_ENV_FILE=<path to .env>.

Run: `BOTAI_ENV_FILE=... BOTAI_LIVE_LLM=1 uv run --frozen pytest -m live_llm -s`.
The key is read by pydantic-settings only; nothing here prints it. A metered cap
(`BOTAI_LIVE_LLM_BUDGET_USD`, default $0.50) stops a runaway test.
"""

from __future__ import annotations

import os

import pytest

from botai.agent.llm import DeepSeekProvider, MeteredLlm
from botai.core.llm import LlmMessage, LlmRequest, LlmTool
from botai.settings import Settings

pytestmark = pytest.mark.live_llm


@pytest.fixture
def live() -> MeteredLlm:
    settings = Settings(llm_provider="deepseek")  # reads BOTAI_ENV_FILE
    if not settings.deepseek_api_key.get_secret_value():
        pytest.skip("DEEPSEEK_API_KEY is not configured (BOTAI_ENV_FILE)")
    cap = float(os.environ.get("BOTAI_LIVE_LLM_BUDGET_USD", "0.50"))
    return MeteredLlm(DeepSeekProvider(settings), cap_usd=cap)


def _report(name: str, meter: MeteredLlm) -> None:
    last = meter.calls[-1].usage
    print(
        f"[L2] {name}: spent=${meter.spent_usd:.5f} calls={len(meter.calls)} "
        f"hit={last.input_cache_hit} miss={last.input_cache_miss} out={last.output} "
        f"reasoning={last.reasoning} latency_ms={last.latency_ms}"
    )


async def test_thinking_disabled_answers_plainly(live: MeteredLlm) -> None:
    out = await live.complete(
        LlmRequest(
            purpose="classify",
            messages=[LlmMessage(role="user", content="Ответь одним словом: столица Франции?")],
            max_tokens=20,
            temperature=0.0,
        )
    )
    _report("thinking disabled", live)
    assert "париж" in out.content.lower()
    assert out.reasoning_content is None
    assert out.usage.output > 0
    assert out.usage.input_cache_hit + out.usage.input_cache_miss > 0


async def test_thinking_with_tool_round_trip(live: MeteredLlm) -> None:
    tool = LlmTool(
        name="get_current_time",
        description="Текущее время студента в формате ЧЧ:ММ.",
        parameters={"type": "object", "properties": {}},
    )
    messages = [
        LlmMessage(role="system", content="Отвечай коротко. Время узнавай только инструментом."),
        LlmMessage(role="user", content="Сколько сейчас времени?"),
    ]
    first = await live.complete(
        LlmRequest(purpose="chat", messages=messages, tools=[tool], max_tokens=200)
    )
    _report("tool call", live)
    assert first.tool_calls, "the model should call the tool"
    print(f"[L2] reasoning_content present on tool call: {bool(first.reasoning_content)}")
    call = first.tool_calls[0]
    assert call.name == "get_current_time"

    messages += [
        LlmMessage(
            role="assistant",
            content=first.content,
            reasoning_content=first.reasoning_content,  # when present, it must go back (else 400)
            tool_calls=first.tool_calls,
        ),
        LlmMessage(role="tool", tool_call_id=call.id, content="<source_data>13:37</source_data>"),
    ]
    second = await live.complete(
        LlmRequest(purpose="chat", messages=messages, tools=[tool], max_tokens=200)
    )
    _report("tool result", live)
    assert "13:37" in second.content
