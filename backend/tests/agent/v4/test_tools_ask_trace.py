"""`ask_user` (callback, secret stays out of the model) and `trace_read` (own and past runs)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from botai.agent.v4 import (
    AgentRuntime,
    MemoryTraceStore,
    RunLimits,
    RunTask,
)
from botai.agent.v4.protocol import AskAnswer, AskQuestion, ToolContext
from botai.agent.v4.providers import MockProvider
from botai.agent.v4.tools.ask import AskUserTool
from botai.agent.v4.tools.traces import TraceReadTool
from botai.agent.v4.traces import TraceEvent
from tests.agent.v4.runtime_support import answer, call, task, tool_response

# --- ask_user -----------------------------------------------------------------------------------


async def test_ask_user_returns_the_answer() -> None:
    asked: list[AskQuestion] = []

    async def ask(question: AskQuestion) -> AskAnswer:
        asked.append(question)
        return AskAnswer(text="да, добавляй")

    result = await AskUserTool().run(
        {"question": "Добавить источник?", "options": ["да", "нет"]},
        ToolContext(run_id="r", ask=ask),
    )
    assert result.ok
    assert result.data["answer"] == "да, добавляй"
    assert asked
    assert asked[0].question == "Добавить источник?"
    assert asked[0].options == ("да", "нет")


async def test_ask_user_choice_maps_the_index_to_the_option() -> None:
    async def ask(question: AskQuestion) -> AskAnswer:
        return AskAnswer(choice=1)

    result = await AskUserTool().run(
        {"question": "Какой вариант?", "options": ["a", "b"], "input_kind": "choice"},
        ToolContext(run_id="r", ask=ask),
    )
    assert result.ok
    assert result.data["chosen"] == "b"


async def test_secret_input_never_reaches_the_model() -> None:
    secret = "P@ssw0rd-FIXTURE"

    async def ask(question: AskQuestion) -> AskAnswer:
        return AskAnswer(text=secret)

    result = await AskUserTool().run(
        {"question": "Пароль от вуза", "input_kind": "secret"},
        ToolContext(run_id="r", ask=ask),
    )
    assert result.ok
    dumped = json.dumps(result.to_wire(), ensure_ascii=False)
    assert secret not in dumped
    assert result.data == {"answered": True, "hidden": True}


async def test_ask_user_without_a_channel_is_an_error_result() -> None:
    result = await AskUserTool().run({"question": "?"}, ToolContext(run_id="r"))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "no_user_channel"


async def test_cancelled_question_is_an_error_result() -> None:
    async def ask(question: AskQuestion) -> AskAnswer:
        return AskAnswer(cancelled=True)

    result = await AskUserTool().run({"question": "?"}, ToolContext(run_id="r", ask=ask))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "ask_cancelled"


async def test_question_timeout_is_retryable() -> None:
    async def ask(question: AskQuestion) -> AskAnswer:
        await asyncio.sleep(1)
        return AskAnswer(text="поздно")

    result = await AskUserTool().run(
        {"question": "?", "timeout_s": 0.05},
        ToolContext(run_id="r", ask=ask),
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "ask_timeout"
    assert result.error.retryable is True


async def test_choice_without_options_is_refused() -> None:
    async def ask(question: AskQuestion) -> AskAnswer:
        raise AssertionError("must not be called")

    result = await AskUserTool().run(
        {"question": "?", "input_kind": "choice"},
        ToolContext(run_id="r", ask=ask),
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "invalid_arguments"


# --- trace_read ---------------------------------------------------------------------------------


def _event(seq: int, kind: str, run_id: str = "run-1", **payload: object) -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        seq=seq,
        at=datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
        kind=kind,  # type: ignore[arg-type]
        payload=dict(payload),
    )


async def _store_with_run() -> MemoryTraceStore:
    store = MemoryTraceStore()
    await store.append(_event(0, "run_started", task="цель"))
    await store.append(_event(1, "model_call", model="m"))
    await store.append(_event(2, "run_finished", status="answer", steps=1))
    return store


async def test_trace_read_returns_the_current_run() -> None:
    store = await _store_with_run()
    result = await TraceReadTool().run({}, ToolContext(run_id="run-1", trace_store=store))
    assert result.ok
    assert result.data["run"]["run_id"] == "run-1"
    assert [event["kind"] for event in result.data["events"]] == [
        "run_started",
        "model_call",
        "run_finished",
    ]
    assert result.data["run"]["status"] == "answer"


async def test_trace_read_pages_and_can_hide_payloads() -> None:
    store = await _store_with_run()
    result = await TraceReadTool().run(
        {"after_seq": 1, "limit": 1, "include_payloads": False},
        ToolContext(run_id="run-1", trace_store=store),
    )
    assert result.ok
    assert len(result.data["events"]) == 1
    assert result.data["events"][0]["seq"] == 1
    assert "payload" not in result.data["events"][0]
    assert result.data["truncated"] is True


async def test_trace_read_lists_runs() -> None:
    store = await _store_with_run()
    result = await TraceReadTool().run(
        {"list_runs": True}, ToolContext(run_id="run-1", trace_store=store)
    )
    assert result.ok
    assert result.data["runs"][0]["run_id"] == "run-1"


async def test_trace_read_reports_missing_run_and_missing_store() -> None:
    store = await _store_with_run()
    missing = await TraceReadTool().run(
        {"run_id": "nope"}, ToolContext(run_id="run-1", trace_store=store)
    )
    assert not missing.ok
    assert missing.error is not None
    assert missing.error.code == "run_not_found"

    no_store = await TraceReadTool().run({}, ToolContext(run_id="run-1"))
    assert not no_store.ok
    assert no_store.error is not None
    assert no_store.error.code == "no_trace_store"


async def test_the_agent_can_read_its_own_trace_inside_a_run() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("trace_read", {})),
        answer("прочитал"),
    )
    runtime = AgentRuntime(monotonic=lambda: 0.0)
    store = MemoryTraceStore()
    run = RunTask(task="задача")
    result = await runtime.run(
        run,
        provider,
        [TraceReadTool()],
        trace_store=store,
        limits=RunLimits(max_steps=5),
    )
    assert result.status == "answer"
    tool_message = provider.requests[1].messages[-1]
    assert "run_started" in str(tool_message.content)
    assert run.run_id in str(tool_message.content)


async def test_ask_user_reaches_the_callback_through_the_runtime() -> None:
    async def ask(question: AskQuestion) -> AskAnswer:
        return AskAnswer(text="ответ владельца")

    provider = MockProvider.from_responses(
        tool_response(call("ask_user", {"question": "нужен ответ?"})),
        answer(),
    )
    runtime = AgentRuntime(monotonic=lambda: 0.0)
    result = await runtime.run(
        task(),
        provider,
        [AskUserTool()],
        trace_store=MemoryTraceStore(),
        limits=RunLimits(max_steps=5),
        ask=ask,
    )
    assert result.status == "answer"
    assert "ответ владельца" in str(provider.requests[1].messages[-1].content)
