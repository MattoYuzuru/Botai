"""Soft limits, emergency stop, pause/cancel and resume of the v4 runtime (L0, D-009 §4.6)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from pydantic import ValidationError

from botai.agent.v4 import (
    AgentRuntime,
    ManualControl,
    MemoryTraceStore,
    RunLimits,
    RunTask,
    ToolResult,
)
from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.providers import MockProvider
from botai.core.llm import LlmUsage
from tests.agent.v4.runtime_support import answer, call, handler_tool, task, tool_response, usage


async def run(
    provider: Any,
    *,
    limits: RunLimits,
    control: ManualControl | None = None,
    pricing: Any = None,
    monotonic: Any = None,
    tools: list[Any] | None = None,
    run_task: RunTask | None = None,
) -> tuple[Any, MemoryTraceStore, AgentRuntime]:
    runtime = AgentRuntime(
        control=control,
        pricing=pricing,
        monotonic=monotonic or (lambda: 0.0),
    )
    store = MemoryTraceStore()
    result = await runtime.run(
        run_task or task(),
        provider,
        tools if tools is not None else [handler_tool("echo")],
        trace_store=store,
        limits=limits,
    )
    return result, store, runtime


def limit_events(result: Any) -> list[dict[str, Any]]:
    return [event.payload for event in result.trace.events if event.kind == "limit"]


# --- step / tokens / cost soft limits -----------------------------------------------------------


async def test_step_soft_limit_stops_with_a_resumable_checkpoint() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"})),
        answer("не должно быть вызвано"),
    )
    result, _, _ = await run(provider, limits=RunLimits(max_steps=1))
    assert result.status == "step_limit"
    assert result.limit_reached == "steps"
    assert result.can_resume
    assert result.answered is False
    assert len(provider.requests) == 1
    assert limit_events(result)[-1]["limit"] == "steps"


async def test_auto_continue_turns_a_soft_limit_into_a_warning() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"})),
        answer("продолжил"),
    )
    result, _, _ = await run(
        provider,
        limits=RunLimits(max_steps=1, auto_continue=True, hard_stop_usd=1.0),
    )
    assert result.status == "answer"
    assert result.content == "продолжил"
    warnings = limit_events(result)
    assert len(warnings) == 1
    assert warnings[0]["limit"] == "steps"
    assert warnings[0]["auto_continue"] is True


async def test_token_soft_limit_counts_all_calls() -> None:
    heavy = usage(miss=1_000, output=20)
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"}), usage=heavy),
        answer(),
    )
    result, _, _ = await run(provider, limits=RunLimits(max_tokens=1_000))
    assert result.status == "token_limit"
    assert result.limit_reached == "tokens"
    assert result.total_tokens == 1_020
    assert len(provider.requests) == 1


async def test_cost_soft_limit_uses_the_injected_pricing() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"})),
        answer(),
    )
    result, _, _ = await run(
        provider,
        limits=RunLimits(max_usd=0.005),
        pricing=lambda usage, at: 0.01,
    )
    assert result.status == "cost_limit"
    assert result.limit_reached == "usd"
    assert result.cost_usd == pytest.approx(0.01)


async def test_hard_stop_wins_over_auto_continue() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"})),
        answer(),
    )
    result, _, _ = await run(
        provider,
        limits=RunLimits(auto_continue=True, hard_stop_usd=0.4),
        pricing=lambda usage, at: 0.5,
    )
    assert result.status == "hard_stop"
    assert result.limit_reached == "hard_stop_usd"
    assert len(provider.requests) == 1
    assert limit_events(result)[-1]["limit"] == "hard_stop_usd"


async def test_time_soft_limit_uses_the_monotonic_clock() -> None:
    ticks = itertools.count(step=1.0)
    provider = MockProvider.from_responses(
        tool_response(call("echo", {"text": "a"})),
        answer(),
    )
    result, _, _ = await run(
        provider,
        limits=RunLimits(max_seconds=2.5),
        monotonic=lambda: next(ticks),
    )
    assert result.status == "time_limit"
    assert result.limit_reached == "seconds"
    assert len(provider.requests) == 1


async def test_no_limits_is_the_default_and_runs_to_the_answer() -> None:
    provider = MockProvider.from_responses(answer())
    result, _, _ = await run(provider, limits=RunLimits())
    assert result.status == "answer"
    assert limit_events(result) == []


# --- pause and cancellation ---------------------------------------------------------------------


async def test_paused_before_the_first_step_returns_a_checkpoint_without_a_model_call() -> None:
    control = ManualControl()
    control.pause()
    provider = MockProvider.from_responses(answer())
    result, store, _ = await run(provider, limits=RunLimits(), control=control)
    assert result.status == "paused"
    assert result.can_resume
    assert provider.requests == []
    assert [message.role for message in result.messages] == ["user"]
    stored = await store.get_run(result.run_id)
    assert stored is not None
    assert stored.status == "paused"
    assert [event.kind for event in result.trace.events] == [
        "run_started",
        "pause",
        "run_finished",
    ]


async def test_cancellation_inside_a_tool_stops_the_run() -> None:
    control = ManualControl()

    async def stop(args: dict[str, Any], context: ToolContext) -> ToolResult:
        control.cancel()
        return ToolResult.success(summary="отменяю")

    provider = MockProvider.from_responses(
        tool_response(call("stop", {"text": "x"})),
        answer("не должно быть вызвано"),
    )
    result, _, _ = await run(
        provider,
        limits=RunLimits(max_steps=10),
        control=control,
        tools=[handler_tool("stop", stop)],
    )
    assert result.status == "cancelled"
    assert len(provider.requests) == 1
    assert [event.kind for event in result.trace.events][-2:] == ["cancel", "run_finished"]


async def test_a_paused_run_can_be_resumed_with_the_same_messages() -> None:
    control = ManualControl()
    control.pause()
    provider = MockProvider.from_responses(answer("после паузы"))
    paused, _, _ = await run(provider, limits=RunLimits(), control=control)
    assert paused.status == "paused"

    resumed_task = RunTask(task="продолжить", messages=paused.messages)
    resumed, _, _ = await run(
        MockProvider.from_responses(answer("продолжение")),
        limits=RunLimits(),
        run_task=resumed_task,
    )
    assert resumed.status == "answer"
    assert resumed.content == "продолжение"


# --- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        {"max_steps": 0},
        {"max_seconds": 0},
        {"max_tokens": 0},
        {"max_usd": 0},
        {"hard_stop_usd": 0},
        {"max_parallel_tools": 0},
        {"max_calls_per_step": 0},
        {"tool_result_chars": 10},
    ],
)
def test_limits_reject_values_that_would_stop_everything(field: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RunLimits(**field)


def test_run_task_requires_a_goal() -> None:
    with pytest.raises(ValidationError):
        RunTask(task="")


def test_default_limits_have_no_walls_but_a_configured_hard_stop() -> None:
    limits = RunLimits()
    assert limits.max_steps is None
    assert limits.max_seconds is None
    assert limits.max_tokens is None
    assert limits.max_usd is None
    assert limits.hard_stop_usd is None
    assert limits.auto_continue is False


# --- accounting details -------------------------------------------------------------------------


async def test_usage_latency_is_measured_when_the_provider_reports_none() -> None:
    no_latency = LlmUsage(input_cache_miss=5, output=1, model="deepseek-flash")
    provider = MockProvider.from_responses(answer(usage=no_latency))
    result, _, _ = await run(
        provider,
        limits=RunLimits(),
        monotonic=lambda: 0.0,
    )
    assert result.usage[0].latency_ms >= 0


async def test_tool_call_limit_can_be_disabled() -> None:
    async def echo(args: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success(data=args)

    provider = MockProvider.from_responses(
        tool_response(
            call("echo", {"text": "a"}, "c1"),
            call("echo", {"text": "b"}, "c2"),
        ),
        answer(),
    )
    result, _, _ = await run(
        provider,
        limits=RunLimits(max_calls_per_step=None, max_parallel_tools=1),
        tools=[handler_tool("echo", echo)],
    )
    assert result.status == "answer"
    assert result.tool_calls == 2


__all__: list[str] = []
