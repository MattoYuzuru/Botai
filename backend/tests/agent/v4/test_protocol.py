"""Tool protocol and traces (L0): strict specs, argument validation, results, registry, store."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from botai.agent.v4.protocol import (
    FunctionTool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    object_schema,
    validate_arguments,
)
from botai.agent.v4.traces import (
    MemoryTraceStore,
    RunInfo,
    RunTrace,
    TraceEvent,
    status_of,
)
from tests.agent.v4.runtime_support import NOW, echo_spec, handler_tool

# --- ToolSpec -----------------------------------------------------------------------------------


def test_spec_has_a_sorted_llm_schema() -> None:
    spec = echo_spec()
    llm_tool = spec.llm_tool()
    assert llm_tool.name == "echo"
    assert llm_tool.parameters["additionalProperties"] is False
    assert llm_tool.parameters["required"] == ["text"]


def test_spec_rejects_a_non_object_schema() -> None:
    with pytest.raises(ValidationError, match="object schema"):
        ToolSpec(name="bad", description="d", parameters={"type": "array"})


def test_spec_rejects_open_schemas() -> None:
    with pytest.raises(ValidationError, match="additionalProperties"):
        ToolSpec(
            name="bad",
            description="d",
            parameters={"type": "object", "properties": {}},
        )


def test_spec_rejects_required_properties_that_are_not_declared() -> None:
    with pytest.raises(ValidationError, match="required properties"):
        ToolSpec(
            name="bad",
            description="d",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": ["missing"],
            },
        )


def test_bad_tool_names_are_refused() -> None:
    for name in ("Echo", "1echo", "echo tool", ""):
        with pytest.raises(ValidationError):
            ToolSpec(name=name, description="d")


def test_object_schema_helper_is_strict() -> None:
    schema = object_schema({"x": {"type": "integer"}}, required=["x"])
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["x"]


# --- validation ---------------------------------------------------------------------------------


def test_validate_arguments_reports_locations() -> None:
    spec = echo_spec()
    assert validate_arguments(spec, {"text": "ok"}) == []
    problems = validate_arguments(spec, {"text": 5, "extra": True})
    assert problems
    assert any("text" in problem for problem in problems)
    assert any("extra" in problem for problem in problems)


def test_validate_arguments_caps_the_list() -> None:
    spec = ToolSpec(
        name="many",
        description="d",
        parameters=object_schema({f"p{index}": {"type": "integer"} for index in range(10)}),
    )
    assert len(validate_arguments(spec, {})) == 0  # nothing is required
    spec_required = ToolSpec(
        name="many_required",
        description="d",
        parameters=object_schema(
            {f"p{index}": {"type": "integer"} for index in range(10)},
            required=[f"p{index}" for index in range(10)],
        ),
    )
    assert len(validate_arguments(spec_required, {})) == 5


# --- ToolResult ---------------------------------------------------------------------------------


def test_success_result_wire_shape() -> None:
    result = ToolResult.success(summary="200 OK", data={"x": 1}, cost_ms=12)
    wire = result.to_wire()
    assert wire["ok"] is True
    assert wire["error"] is None
    assert wire["cost"] == {"ms": 12}
    assert '"x": 1' in result.to_model_text()


def test_failure_result_requires_an_error_and_ok_result_forbids_one() -> None:
    with pytest.raises(ValidationError, match="must carry an error"):
        ToolResult(ok=False, summary="нет")
    with pytest.raises(ValidationError, match="must not carry an error"):
        ToolResult(ok=True, error={"code": "x"})


def test_failure_result_is_structured() -> None:
    result = ToolResult.failure("timeout", "не успел", retryable=True, hint="повторите")
    assert result.ok is False
    assert result.error is not None
    assert (result.error.code, result.error.retryable, result.error.hint) == (
        "timeout",
        True,
        "повторите",
    )


# --- registry and handlers ----------------------------------------------------------------------


def test_registry_keeps_schemas_sorted_and_refuses_duplicates() -> None:
    registry = ToolRegistry([handler_tool("zulu"), handler_tool("alpha")])
    assert [spec.name for spec in registry.specs] == ["alpha", "zulu"]
    assert [tool.name for tool in registry.llm_tools()] == ["alpha", "zulu"]
    assert "alpha" in registry
    assert registry.get("missing") is None
    assert registry.validate("missing", {}) == ["unknown tool: missing"]
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([handler_tool("echo"), handler_tool("echo")])


async def test_function_tool_adapts_a_plain_function() -> None:
    async def run(args: dict[str, object], context: ToolContext) -> ToolResult:
        return ToolResult.success(data=args)

    tool = FunctionTool(echo_spec(), run)
    context = ToolContext(run_id="r1")
    result = await tool.run({"text": "hi"}, context)
    assert result.data == {"text": "hi"}


# --- traces -------------------------------------------------------------------------------------


def _event(seq: int, kind: str, run_id: str = "run-1", **payload: object) -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        seq=seq,
        at=datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
        kind=kind,  # type: ignore[arg-type]
        payload=dict(payload),
    )


async def test_memory_store_assembles_a_run_trace() -> None:
    store = MemoryTraceStore()
    await store.append(_event(0, "run_started", task="цель", provider="mock", model="m"))
    await store.append(
        _event(
            1,
            "model_call",
            model="m",
            input_cache_miss=10,
            input_cache_hit=3,
            output=2,
        )
    )
    await store.append(
        _event(2, "run_finished", status="answer", steps=1, total_tokens=15, cost_usd=0.5)
    )
    trace = await store.get_run("run-1")
    assert trace is not None
    assert trace.task == "цель"
    assert trace.status == "answer"
    assert trace.steps == 1
    assert trace.total_tokens == 15
    assert trace.cost_usd == 0.5
    assert [event.kind for event in trace.events] == [
        "run_started",
        "model_call",
        "run_finished",
    ]


async def test_memory_store_read_is_paged_and_unknown_run_is_none() -> None:
    store = MemoryTraceStore()
    for seq in range(5):
        await store.append(_event(seq, "note", text=str(seq)))
    assert [event.seq for event in await store.read("run-1", after_seq=2, limit=2)] == [2, 3]
    assert await store.read("run-1", after_seq=10) == []
    assert await store.get_run("other") is None


async def test_memory_store_lists_runs_newest_first() -> None:
    store = MemoryTraceStore()
    await store.append(
        TraceEvent(
            run_id="old",
            seq=0,
            at=datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
            kind="run_started",
            payload={"task": "старый"},
        )
    )
    await store.append(
        TraceEvent(
            run_id="new",
            seq=0,
            at=datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
            kind="run_started",
            payload={"task": "новый"},
        )
    )
    runs = await store.list_runs(limit=10)
    assert [run.run_id for run in runs] == ["new", "old"]
    assert isinstance(runs[0], RunInfo)


async def test_status_of_ignores_a_broken_payload() -> None:
    assert status_of({"status": "answer"}) == "answer"
    assert status_of({"status": "not-a-status"}) == "running"
    assert status_of({}) == "running"


def test_run_trace_is_frozen_and_dated() -> None:
    trace = RunTrace(run_id="r1", started_at=NOW, finished_at=NOW)
    assert trace.status == "running"
    with pytest.raises(ValidationError):
        trace.run_id = "other"  # type: ignore[misc]
