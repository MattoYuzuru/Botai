"""Activity is visible before completion, remains timed, and does not leak tool payloads."""

from datetime import UTC, datetime, timedelta

from botai.agent.v4.progress import JobTraceStore
from botai.agent.v4.traces import MemoryTraceStore, TraceEvent
from botai.core.jobs import InMemoryJobContext


async def test_tool_progress_is_live_and_keeps_start_time_without_sensitive_payloads() -> None:
    ctx = InMemoryJobContext()
    inner = MemoryTraceStore()
    store = JobTraceStore(inner, ctx)
    at = datetime(2026, 11, 1, tzinfo=UTC)
    await store.append(
        TraceEvent(
            run_id="run",
            seq=0,
            kind="tool_call",
            at=at,
            payload={
                "call_id": "c1",
                "tool": "source_read_live",
                "arguments": {
                    "url": "https://login:private@university.test/private?token=secret-value",
                    "password": "sensitive-value",
                },
            },
        )
    )
    block = ctx.blocks[0]
    assert block.type == "thinking"
    assert block.state == "running"
    assert block.stage == "Читаю источник: university.test"
    assert "private" not in block.model_dump_json()
    assert "secret-value" not in block.model_dump_json()
    assert "sensitive-value" not in block.model_dump_json()
    await store.append(
        TraceEvent(
            run_id="run",
            seq=1,
            kind="tool_result",
            at=at + timedelta(seconds=12),
            payload={"call_id": "c1", "ok": False, "summary": "secret-value"},
        )
    )
    done = ctx.blocks[0]
    assert done.type == "thinking"
    assert done.id == block.id
    assert done.at == at
    assert done.state == "failed"
    assert "secret-value" not in done.model_dump_json()
    assert len(await store.read("run")) == 2


async def test_parallel_tools_finish_their_own_progress_and_show_record_count() -> None:
    ctx = InMemoryJobContext()
    store = JobTraceStore(MemoryTraceStore(), ctx)
    at = datetime(2026, 11, 1, tzinfo=UTC)
    for seq, call_id in enumerate(("a", "b")):
        await store.append(
            TraceEvent(
                run_id="run",
                seq=seq,
                kind="tool_call",
                at=at,
                payload={"call_id": call_id, "tool": "source_probe"},
            )
        )
    first_id = ctx.blocks[0].id
    await store.append(
        TraceEvent(
            run_id="run",
            seq=2,
            kind="tool_result",
            at=at,
            payload={
                "call_id": "b",
                "ok": True,
                "result": {"data": {"report": {"counts": {"schedule": 4}}}},
            },
        )
    )
    assert ctx.blocks[0].id == first_id
    assert ctx.blocks[0].type == "thinking"
    assert ctx.blocks[0].state == "running"
    assert ctx.blocks[1].type == "thinking"
    assert ctx.blocks[1].detail == "Получено записей: 4"
