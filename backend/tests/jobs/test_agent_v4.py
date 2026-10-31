"""DB-backed wiring: the agent trace store and an `AGENT` job through the real worker (Postgres).

No browser here: these tests cover the queue/DB side of the experiment (`_browser=False` on the
payload) — the run still uses the real executor and the real trace table. Chromium runs are covered
by tests/agent/v4/test_agent_run_e2e.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from botai.agent.v4.factory import enqueue_agent_run
from botai.agent.v4.handlers import make_agent_handlers
from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.providers import MockProvider
from botai.agent.v4.tools.traces import TraceReadTool
from botai.agent.v4.trace_store import DbTraceStore
from botai.agent.v4.traces import TraceEvent
from botai.core.errors import ErrorCode
from botai.db.models import AgentRun, AgentRunEvent
from tests.agent.v4.runtime_support import answer, call, tool_response
from tests.jobs.env import Env

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def event(run_id: str, seq: int, kind: str, **payload: object) -> TraceEvent:
    return TraceEvent(run_id=run_id, seq=seq, at=NOW, kind=kind, payload=dict(payload))  # type: ignore[arg-type]


# --- DbTraceStore ------------------------------------------------------------------------------


async def test_db_trace_store_round_trip(env: Env) -> None:
    user = await env.account()
    store = DbTraceStore(env.sm, user_id=user.id)
    run_id = "01M68Z000000000000000000C0"

    await store.append(
        event(run_id, 0, "run_started", task="найди расписание", provider="mock", model="m")
    )
    await store.append(event(run_id, 1, "model_call", step=0, total_tokens=0))
    await store.append(
        event(
            run_id,
            2,
            "run_finished",
            status="answer",
            steps=1,
            total_tokens=10,
            cost_usd=0.01,
        )
    )
    # A replayed event (same seq) is written once: the runtime may append after a crash.
    await store.append(event(run_id, 1, "model_call", step=0, total_tokens=0))

    assert [item.seq for item in await store.read(run_id)] == [0, 1, 2]
    assert [item.seq for item in await store.read(run_id, after_seq=1)] == [1, 2]
    trace = await store.get_run(run_id)
    assert trace is not None
    assert trace.status == "answer"
    assert trace.task == "найди расписание"
    assert trace.steps == 1
    assert trace.total_tokens == 10
    assert trace.cost_usd == 0.01
    assert trace.started_at is not None
    assert trace.finished_at is not None
    assert [item.kind for item in trace.events] == ["run_started", "model_call", "run_finished"]

    runs = await store.list_runs()
    assert [run.run_id for run in runs] == [run_id]
    assert runs[0].event_count == 3
    assert runs[0].status == "answer"

    # The agent's own trace_read tool sees the same trace through the store protocol.
    read = await TraceReadTool().run(
        {"run_id": run_id}, ToolContext(run_id=run_id, trace_store=store)
    )
    assert read.ok
    assert read.data["run"]["status"] == "answer"
    assert [item["seq"] for item in read.data["events"]] == [0, 1, 2]

    other = DbTraceStore(env.sm, user_id=(await env.account()).id)
    assert await other.get_run(run_id) is None
    assert await other.list_runs() == []


async def test_a_late_trace_event_still_creates_its_run(env: Env) -> None:
    user = await env.account()
    store = DbTraceStore(env.sm, user_id=user.id)
    await store.append(event("01M68Z000000000000000000C1", 0, "note", text="interrupted"))
    trace = await store.get_run("01M68Z000000000000000000C1")
    assert trace is not None
    assert trace.status == "running"
    assert trace.events[0].payload == {"text": "interrupted"}


# --- the AGENT job -----------------------------------------------------------------------------


async def test_agent_job_runs_code_and_stores_its_trace(env: Env, tmp_path: Path) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    workspace = tmp_path / "agent-task"
    provider = MockProvider(
        responses=[
            tool_response(
                call(
                    "code_exec",
                    {"language": "python", "code": "open('result.txt', 'w').write('2 пары')"},
                    "c1",
                )
            ),
            tool_response(call("fs_read", {"path": "result.txt"}, "c2")),
            answer("Результат сохранён в result.txt"),
        ]
    )
    job_id = await enqueue_agent_run(
        env.services,
        user_id=user.id,
        task="открой кабинет и сохрани расписание",
        payload={"_browser": False, "_workspace_root": str(workspace)},
    )
    env.start(env.worker(make_agent_handlers(provider=provider)))
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert job.result is not None
    assert job.result["status"] == "answer"
    assert job.result["steps"] == 3
    assert job.result["tool_calls"] == 2
    assert "result.txt" in job.result["content"]
    used_workspace = Path(job.result["workspace"])
    assert used_workspace.is_relative_to(tmp_path)
    assert (used_workspace / "result.txt").read_text(encoding="utf-8") == "2 пары"

    async with env.sm() as session:
        runs = list(await session.scalars(select(AgentRun).where(AgentRun.user_id == user.id)))
        assert len(runs) == 1
        run = runs[0]
        assert run.run_id == job.result["run_id"]
        assert run.status == "answer"
        assert run.provider == "mock"
        assert run.finished_at is not None
        events = list(
            await session.scalars(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run.run_id)
                .order_by(AgentRunEvent.seq)
            )
        )
        assert len(events) == run.event_count
        assert events[0].kind == "run_started"
        assert events[-1].kind == "run_finished"
        assert {"model_call", "tool_call", "tool_result"} <= {item.kind for item in events}
        # Traces are structural data: no exec environment or provider key material in them.
        dumped = json.dumps([item.payload for item in events], ensure_ascii=False)
        assert "BOTAI_KEK" not in dumped
        assert "DATABASE_URL" not in dumped


async def test_agent_job_is_refused_while_the_flag_is_v3(env: Env, tmp_path: Path) -> None:
    user = await env.account()
    job_id = await enqueue_agent_run(
        env.services,
        user_id=user.id,
        task="проверка",
        payload={"_browser": False, "_workspace_root": str(tmp_path / "ws")},
    )
    env.start(env.worker(make_agent_handlers(provider=MockProvider())))
    job = await env.wait_for(job_id, {"failed"}, within=30)

    assert job.error is not None
    assert job.error["code"] == ErrorCode.VALIDATION.value
    assert "BOTAI_AGENT_ENGINE=v4" in job.error["detail"]
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.user_id == user.id)
            )
            == 0
        )
