"""Checkpoint sink of the v4 runtime (L0, D-040): cadence, consistency, masking, resumable errors."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from botai.agent.v4 import AgentRuntime, MemoryTraceStore, RunLimits
from botai.agent.v4.protocol import ToolContext, ToolResult
from botai.agent.v4.providers import MockProvider
from botai.agent.v4.runtime import (
    INTERRUPTED_TOOL_TEXT,
    CheckpointSnapshot,
    has_side_effects,
    is_resumable,
    repair_dangling_tool_calls,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import LlmMessage, LlmRequest, LlmResponse, LlmToolCall
from tests.agent.v4.runtime_support import (
    answer,
    call,
    handler_tool,
    task,
    tool_response,
    usage,
)

SECRET = "P@ssw0rd-FIXTURE-MARKER"


class Sink:
    def __init__(self) -> None:
        self.snapshots: list[CheckpointSnapshot] = []

    async def save(self, snapshot: CheckpointSnapshot) -> None:
        self.snapshots.append(snapshot)


class RaisingSink:
    async def save(self, snapshot: CheckpointSnapshot) -> None:
        raise RuntimeError("database is down")


def runtime(**kw: Any) -> AgentRuntime:
    kw.setdefault("monotonic", lambda: 0.0)
    return AgentRuntime(**kw)


def mask_secret(text: str) -> str:
    return text.replace(SECRET, "***")


def assert_consistent(messages: tuple[LlmMessage, ...]) -> None:
    """Every assistant tool call has exactly one tool answer right after it; no orphan answers."""
    index = 0
    while index < len(messages):
        message = messages[index]
        assert message.role != "system"
        if message.role == "tool":
            raise AssertionError(f"orphan tool message at {index}")
        index += 1
        if message.role == "assistant" and message.tool_calls:
            wanted = [item.id for item in message.tool_calls]
            got = []
            while index < len(messages) and messages[index].role == "tool":
                got.append(messages[index].tool_call_id)
                index += 1
            assert got == wanted


async def test_the_sink_gets_a_snapshot_after_each_step_and_a_final_one() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"}, "c1")),
        tool_response(call("echo", {"text": "b"}, "c2")),
        answer("готово"),
    )
    sink = Sink()
    result = await runtime(checkpoint_every_steps=1).run(
        task(), provider, [handler_tool("echo")], checkpoint_sink=sink
    )
    assert result.status == "answer"
    assert [snapshot.final for snapshot in sink.snapshots] == [False, False, True]
    assert [snapshot.steps for snapshot in sink.snapshots] == [1, 2, 3]
    assert sink.snapshots[0].status == "running"
    assert sink.snapshots[-1].status == "answer"
    assert sink.snapshots[-1].resumable is False
    assert sink.snapshots[-1].messages == result.messages
    for snapshot in sink.snapshots:
        assert_consistent(snapshot.messages)


async def test_snapshots_are_throttled_by_steps_and_seconds_but_the_final_one_is_not() -> None:
    provider = MockProvider.from_responses(
        *[tool_response(call("echo", {"text": str(n)}, f"c{n}")) for n in range(5)],
        answer("готово"),
    )
    sink = Sink()
    await runtime(checkpoint_every_steps=3, checkpoint_every_seconds=1000).run(
        task(), provider, [handler_tool("echo")], checkpoint_sink=sink
    )
    # steps 3 (>= 3 since the last save) and then the final snapshot
    assert [(s.steps, s.final) for s in sink.snapshots] == [(3, False), (6, True)]

    clock = iter(range(0, 1000, 25))  # every reading is 25 s after the previous one
    timed = Sink()
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"}, "c1")),
        tool_response(call("echo", {"text": "b"}, "c2")),
        answer("готово"),
    )
    await AgentRuntime(
        monotonic=lambda: float(next(clock)),
        checkpoint_every_steps=100,
        checkpoint_every_seconds=20,
    ).run(task(), provider, [handler_tool("echo")], checkpoint_sink=timed)
    assert [s.final for s in timed.snapshots] == [False, False, True]


async def test_a_failing_sink_never_fails_the_run() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"})), answer("готово")
    )
    result = await runtime(checkpoint_every_steps=1).run(
        task(), provider, [handler_tool("echo")], checkpoint_sink=RaisingSink()
    )
    assert result.status == "answer"
    assert result.content == "готово"


async def test_a_step_limit_snapshot_is_resumable_and_final() -> None:
    provider = MockProvider.from_responses(tool_response(call("echo", {"text": "a"})), answer("x"))
    sink = Sink()
    result = await runtime().run(
        task(),
        provider,
        [handler_tool("echo")],
        limits=RunLimits(max_steps=1),
        checkpoint_sink=sink,
    )
    assert result.status == "step_limit"
    final = sink.snapshots[-1]
    assert final.final
    assert final.resumable
    assert final.status == "step_limit"
    assert final.limit_reached == "steps"
    assert final.messages == result.messages


async def test_a_dangling_tool_call_is_closed_when_the_run_is_cancelled() -> None:
    started = asyncio.Event()

    async def hang(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        started.set()
        await asyncio.sleep(3600)
        return ToolResult.success(summary="never")

    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"}, "c1"), call("echo", {"text": "b"}, "c2")),
        answer("не будет"),
    )
    sink = Sink()
    job = asyncio.create_task(
        runtime().run(
            task(),
            provider,
            [handler_tool("echo", hang)],
            checkpoint_sink=sink,
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job

    final = sink.snapshots[-1]
    assert final.final
    assert final.status == "cancelled"
    assert final.resumable
    assert_consistent(final.messages)
    answers = [m for m in final.messages if m.role == "tool"]
    assert [m.tool_call_id for m in answers] == ["c1", "c2"]
    assert all(m.content == INTERRUPTED_TOOL_TEXT for m in answers)


async def test_an_unexpected_exception_leaves_a_consistent_snapshot() -> None:
    class Exploding:
        name = "exploding"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request: LlmRequest) -> LlmResponse:
            self.calls += 1
            if self.calls == 1:
                return tool_response(call("echo", {"text": "a"}, "c1"))
            raise ZeroDivisionError

        async def aclose(self) -> None:
            return None

    sink = Sink()
    result = await runtime(checkpoint_every_steps=1).run(
        task(), Exploding(), [handler_tool("echo")], checkpoint_sink=sink
    )
    # a provider bug is a provider error: llm_error, resumable, with the finished step kept
    assert result.status == "error"
    assert result.error_code == "llm_error"
    assert result.can_resume
    assert_consistent(result.messages)
    assert sink.snapshots[-1].resumable
    assert [m.role for m in sink.snapshots[-1].messages][-2:] == ["assistant", "tool"]


def test_repair_closes_calls_and_drops_orphans() -> None:
    messages = [
        LlmMessage(role="user", content="привет"),
        LlmMessage(
            role="assistant",
            tool_calls=[
                LlmToolCall(id="a", name="echo", arguments={}),
                LlmToolCall(id="b", name="echo", arguments={}),
            ],
        ),
        LlmMessage(role="tool", tool_call_id="b", content="B"),
        LlmMessage(role="tool", tool_call_id="zzz", content="orphan"),
        LlmMessage(role="user", content="дальше"),
        LlmMessage(role="tool", tool_call_id="late", content="orphan 2"),
    ]
    repaired = repair_dangling_tool_calls(messages)
    assert [m.role for m in repaired] == ["user", "assistant", "tool", "tool", "user"]
    assert [m.tool_call_id for m in repaired if m.role == "tool"] == ["a", "b"]
    assert repaired[2].content == INTERRUPTED_TOOL_TEXT
    assert repaired[3].content == "B"
    assert repair_dangling_tool_calls(repaired) == repaired  # idempotent


async def test_reasoning_content_and_arguments_are_masked_in_the_checkpoint() -> None:
    provider = MockProvider.from_responses(
        tool_response(
            call("echo", {"text": f"login {SECRET}"}, "c1"),
            content=f"пишу {SECRET}",
            reasoning_content=f"рассуждаю про {SECRET}",
        ),
        answer("готово"),
    )
    sink = Sink()
    result = await runtime(mask=mask_secret, checkpoint_every_steps=1).run(
        task(), provider, [handler_tool("echo")], checkpoint_sink=sink
    )
    dumped = json.dumps(
        [m.model_dump(mode="json") for s in sink.snapshots for m in s.messages]
        + [m.model_dump(mode="json") for m in result.messages],
        ensure_ascii=False,
    )
    assert SECRET not in dumped
    assistant = next(m for m in result.messages if m.role == "assistant" and m.tool_calls)
    assert assistant.reasoning_content == "рассуждаю про ***"


@pytest.mark.parametrize("code", ["llm_unavailable", "quota_exceeded", "llm_error"])
async def test_provider_errors_are_resumable_and_keep_the_conversation(code: str) -> None:
    class Failing:
        name = "failing"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request: LlmRequest) -> LlmResponse:
            self.calls += 1
            if self.calls == 1:
                return tool_response(call("echo", {"text": "a"}, "c1"), usage=usage())
            if code == "llm_error":
                raise RuntimeError("boom")
            raise BotaiError(
                ErrorCode(code),
                "нет",
                extra={"scope": "minute", "resetsAt": "2026-10-31T12:01:00Z"},
            )

        async def aclose(self) -> None:
            return None

    sink = Sink()
    result = await runtime().run(task(), Failing(), [handler_tool("echo")], checkpoint_sink=sink)
    assert result.status == "error"
    assert result.error_code == code
    assert result.can_resume
    assert is_resumable("error", code)
    if code != "llm_error":
        assert result.error_extra == {"scope": "minute", "resetsAt": "2026-10-31T12:01:00Z"}
    final = sink.snapshots[-1]
    assert final.resumable
    assert final.error_code == code
    assert [m.role for m in final.messages][-2:] == ["assistant", "tool"]


def test_only_the_listed_errors_are_resumable() -> None:
    assert not is_resumable("error", "internal_error")
    assert not is_resumable("error", None)
    assert not is_resumable("answer", None)
    assert is_resumable("cancelled", None)
    assert is_resumable("hard_stop", None)


async def test_the_resumed_run_continues_from_the_error_checkpoint_without_repeating_the_task() -> (
    None
):
    class Down:
        name = "down"
        calls = 0

        async def complete(self, request: LlmRequest) -> LlmResponse:
            self.calls += 1
            if self.calls == 1:
                return tool_response(call("echo", {"text": "a"}, "c1"))
            raise BotaiError(ErrorCode.LLM_UNAVAILABLE, "модель недоступна")

        async def aclose(self) -> None:
            return None

    failed = await runtime().run(task(), Down(), [handler_tool("echo")])
    assert failed.status == "error"
    assert failed.can_resume

    resumed_provider = MockProvider.from_responses(answer("доделал"))
    resumed = await runtime().run(
        task(messages=failed.messages, task="Продолжение 2"),
        resumed_provider,
        [handler_tool("echo")],
    )
    assert resumed.status == "answer"
    sent = resumed_provider.requests[0].messages
    assert [m.role for m in sent] == ["system", "user", "user", "assistant", "tool", "user"]
    assert sent[-1].content == "Продолжение 2"
    assert len([m for m in sent if m.content == "проверка цикла"]) == 1  # the old task, once


async def test_a_segment_hard_stop_override_stops_the_run_before_the_next_model_call() -> None:
    expensive = usage(miss=1_000_000)  # about $0.15 at the flash tariff
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"}, "c1"), usage=expensive),
        tool_response(call("echo", {"text": "b"}, "c2"), usage=expensive),
        answer("не должно быть вызвано"),
    )
    store = MemoryTraceStore()
    result = await runtime().run(
        task(),
        provider,
        [handler_tool("echo")],
        trace_store=store,
        limits=RunLimits(hard_stop_usd=0.10),  # what is left of the chain's money cap
    )
    assert result.status == "hard_stop"
    assert result.limit_reached == "hard_stop_usd"
    assert len(provider.requests) == 1
    assert result.can_resume


async def test_a_step_with_a_side_effect_is_saved_at_once_whatever_the_cadence() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "read"}, "c1")),
        tool_response(call("action_execute", {"text": "send"}, "c2")),
        tool_response(call("echo", {"text": "read again"}, "c3")),
        answer("готово"),
    )
    sink = Sink()
    await runtime(checkpoint_every_steps=100, checkpoint_every_seconds=1000).run(
        task(),
        provider,
        [handler_tool("echo"), handler_tool("action_execute")],
        checkpoint_sink=sink,
    )
    # only the step that executed the action was saved before the final snapshot
    assert [(s.steps, s.final) for s in sink.snapshots] == [(2, False), (4, True)]
    calls = [c.name for m in sink.snapshots[0].messages for c in m.tool_calls]
    assert calls == ["echo", "action_execute"]
    assert sink.snapshots[0].messages[-1].role == "tool"  # the result of the effect is in it


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("action_execute", {}, True),
        ("action_propose", {}, True),
        ("source_sync", {"source_id": "s"}, True),
        ("source_update_playbook", {}, True),
        ("ui_propose_panel", {}, True),
        ("http_request", {"url": "https://x.example/a", "method": "POST"}, True),
        ("http_request", {"url": "https://x.example/a", "method": " delete "}, True),
        ("http_request", {"url": "https://x.example/a", "method": "get"}, False),
        ("http_request", {"url": "https://x.example/a", "method": "HEAD"}, False),
        ("http_request", {"url": "https://x.example/a"}, False),
        ("source_read", {}, False),
        ("fs_write", {}, False),
    ],
)
def test_which_calls_have_side_effects(
    name: str, arguments: dict[str, Any], expected: bool
) -> None:
    assert has_side_effects(LlmToolCall(id="c", name=name, arguments=arguments)) is expected
