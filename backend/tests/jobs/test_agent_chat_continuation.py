"""CHAT-v4 continuity on Postgres (D-040): segments of one job, checkpoints, stops, restarts, cancel.

The model is a scripted provider that also writes `llm_usage` like the real one, the tools are the
real ones (a real workspace), the worker is the real `Worker`. Every scenario is what the student
or the operator would see: one job that goes on by itself, a stop that says why and keeps the work,
a next message that resumes from the work instead of re-reading traces.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text, update

from botai.agent.llm import current_scope
from botai.agent.v4 import chat as chat_module
from botai.agent.v4.chat import make_chat_handlers
from botai.agent.v4.checkpoints import SegmentCheckpointer, checkpoint_store
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.core.llm import LlmMessage, LlmRequest, LlmResponse
from botai.db.models import (
    AgentRun,
    AgentRunCheckpoint,
    AgentRunEvent,
    ChatMessage,
    Job,
    JobEvent,
    LlmUsageRow,
)
from botai.jobs.runner import Worker
from botai.usage import service as usage_service
from tests.agent.v4.runtime_support import answer, call, tool_response, usage
from tests.jobs.env import Env
from tests.jobs.test_agent_chat_v4 import FlowProvider, _post, _steps, _student

pytestmark = pytest.mark.db

Respond = Callable[[LlmRequest], Awaitable[LlmResponse]]
BIG = usage(miss=1_000_000)  # about $0.15 at the flash tariff (off-peak, like the test clock)


class MeteredProvider(FlowProvider):
    """Writes `llm_usage` for its job like the real provider does inside the worker's scope."""

    def __init__(self, env: Env, respond: Respond) -> None:
        super().__init__(respond)
        self._env = env

    async def complete(self, request: LlmRequest) -> LlmResponse:
        response = await super().complete(request)
        scope = current_scope()
        if scope is not None:
            async with self._env.sm() as session:
                await usage_service.record_usage(
                    session,
                    user_id=scope.user_id,
                    job_id=scope.job_id,
                    purpose="agent",
                    usage=response.usage,
                    peak=False,
                    now=self._env.clock.now(),
                )
        return response


def step_tool(n: int, *, big: bool = False) -> LlmResponse:
    return tool_response(
        call("fs_write", {"path": f"f{n}.txt", "content": f"payload-{n}"}, f"c{n}"),
        usage=BIG if big else usage(),
    )


def tool_calls_of(request: LlmRequest) -> list[tuple[str, str]]:
    return [(c.name, c.id) for m in request.messages for c in m.tool_calls]


def assert_continues_from_checkpoint(request: LlmRequest, expected_calls: list[str]) -> None:
    """The request carries the saved conversation and tells the model not to read the traces."""
    assert request.messages[0].role == "system"
    # the system prompt keeps trace_read to a spot check of one step (D-040), never a way to continue
    assert "trace_read нужен только для точечной проверки" in request.messages[0].content
    assert [name for name, _ in tool_calls_of(request)] == expected_calls
    instruction = request.messages[-1]
    assert instruction.role == "user"
    assert "не перечитыва" in instruction.content  # «не перечитывай/не перечитывая трассы»
    assert "trace_read" not in instruction.content
    assert not any(
        "trace_read" in m.content for m in request.messages[1:] if m.role in ("tool", "assistant")
    )


def settings(env: Env, **values: Any) -> None:
    env.settings.agent_engine = "v4"
    env.settings.agent_checkpoint_every_steps = 1
    for name, value in values.items():
        setattr(env.settings, name, value)


async def start_worker(env: Env, provider: FlowProvider, tmp_path: Path, **config: Any) -> Worker:
    worker = env.worker(
        make_chat_handlers(
            provider=provider, artifact_root=str(tmp_path / "artifacts"), with_browser=False
        ),
        **config,
    )
    env.start(worker)
    return worker


async def wait_until(
    predicate: Callable[[], Awaitable[bool]], *, within: float = 15.0, what: str = "condition"
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not await predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def wait_queued(env: Env, job_id: str, stage: str, *, within: float = 15.0) -> Job:
    async def ready() -> bool:
        job = await env.job(job_id)
        return job.state == "queued" and (job.stage or "").startswith(stage)

    await wait_until(ready, within=within, what=f"job queued with stage {stage!r}")
    return await env.job(job_id)


async def agent_texts(env: Env, thread: str, job_id: str) -> list[ChatMessage]:
    async with env.sm() as session:
        rows = list(
            await session.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.thread_id == thread,
                    ChatMessage.job_id == job_id,
                    ChatMessage.role == "agent",
                )
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        )
    return [row for row in rows if row.blocks and row.blocks[0].get("type") == "text"]


async def spent(env: Env, job_id: str) -> float:
    async with env.sm() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(LlmUsageRow.cost_usd), 0)).where(
                LlmUsageRow.job_id == job_id
            )
        )
    return float(total or 0)


async def checkpoints_of(env: Env, job_id: str) -> list[AgentRunCheckpoint]:
    async with env.sm() as session:
        return list(
            await session.scalars(
                select(AgentRunCheckpoint)
                .where(AgentRunCheckpoint.job_id == job_id)
                .order_by(AgentRunCheckpoint.segment)
            )
        )


# --- the chain goes on by itself --------------------------------------------------------------------


async def test_two_automatic_continuations_finish_one_job_with_one_reply(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings(env, agent_limits_steps=3, agent_continuations_max=2)
    client, _user, thread, _space = await _student(env)
    facts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        chat_module, "diagnostic_event", lambda name, **kw: facts.append({"event": name, **kw})
    )

    async def respond(request: LlmRequest) -> LlmResponse:
        n = _steps(request)
        if n <= 6:
            return step_tool(n)
        if n == 7:  # a file written by the first segment is still in the workspace
            return tool_response(call("fs_read", {"path": "f0.txt"}, "read0"))
        return answer("Готово: всё собрано за три части.")

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Собери всё по расписанию")
    assert sent.status_code == 202, sent.text
    job_id = sent.json()["jobId"]
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(job_id, {"done"}, within=30)

    result = job.result
    assert result is not None
    assert result["stopped"] == "answer"
    assert result["chain"]["segments"] == 3
    assert result["chain"]["continuations"] == 2
    assert "resume" not in result
    assert result["steps"] == 9
    assert {"fs_write", "fs_read"} <= set(result["tools"])
    assert result["cost_usd"] == pytest.approx(await spent(env, job_id), abs=1e-5)
    assert await spent(env, job_id) > 0
    assert len(provider.requests) == 9
    assert job.attempts == 3  # one claim per segment, no failure counted
    assert job.payload["_retry_failures"] == 0

    # one stored reply for the job, however many segments it took
    replies = await agent_texts(env, thread, job_id)
    assert len(replies) == 1
    assert replies[0].blocks[0]["markdown"] == "Готово: всё собрано за три части."

    # the second segment starts from the first one's work: its tool calls are in the request, the
    # instruction says so, and no request of the chain reads the traces
    second_start = provider.requests[3]
    assert [name for name, _ in tool_calls_of(second_start)] == ["fs_write"] * 3
    assert second_start.messages[-1].role == "user"
    assert second_start.messages[-1].content.startswith("Это продолжение твоего хода (часть 2)")
    assert_continues_from_checkpoint(second_start, ["fs_write"] * 3)
    assert "Это продолжение твоего хода (часть 2)" in second_start.messages[-1].content
    # the workspace is shared by the segments: the file of the first one is read by the last
    final_tools = [m.content for m in provider.requests[8].messages if m.role == "tool"]
    assert any("payload-0" in content for content in final_tools)

    # the student sees the automatic continuations in the chat, and the job told its stage
    events = await env.events(job_id)
    stages = [e.data.get("stage") or "" for e in events if e.type in ("status", "block")]
    assert any("Продолжаю автоматически (1 из 2): достигнут лимит шагов." in s for s in stages)
    assert any("Продолжаю автоматически (2 из 2): достигнут лимит шагов." in s for s in stages)

    # checkpoints: one per segment, chained; the turn is answered, so none of them is resumable
    rows = await checkpoints_of(env, job_id)
    assert [r.segment for r in rows] == [0, 1, 2]
    assert [r.parent_run_id for r in rows] == [None, rows[0].run_id, rows[1].run_id]
    assert [r.status for r in rows] == ["step_limit", "step_limit", "answer"]
    assert [r.resumable for r in rows] == [False, False, False]
    assert len({r.chain_id for r in rows}) == 1
    assert rows[0].chain_id == job_id
    assert rows[2].cost_usd_chain > rows[0].cost_usd_chain

    # per-segment metrics: a diagnostic event and a continuation note in the trace
    segments = [f for f in facts if f["event"] == "chain.segment"]
    assert [f["segment"] for f in segments] == [0, 1, 2]
    assert [f["decision"] for f in segments] == ["continue", "continue", "stop"]
    for key in (
        "input_tokens_at_start",
        "tokens",
        "cost_usd",
        "trace_read_calls",
        "checkpoint_bytes",
        "message_count",
        "compacted",
    ):
        assert key in segments[0]
    assert all(f["trace_read_calls"] == 0 for f in segments)
    assert segments[1]["message_count"] > segments[0]["message_count"]
    async with env.sm() as session:
        stored = await session.scalars(
            select(AgentRunEvent.payload)
            .join(AgentRun, AgentRun.run_id == AgentRunEvent.run_id)
            .where(AgentRun.job_id == job_id, AgentRunEvent.kind == "note")
        )
        # The test clock is frozen, so `started_at` ties: the segments order the notes, not the clock.
        notes = sorted(stored, key=lambda note: note["segment"])
    assert [n["decision"] for n in notes] == ["continue", "continue", "stop"]
    assert notes[1]["resumed_from"] == "checkpoint"
    assert notes[0]["resumed_from"] == "history"


async def test_a_segment_ends_with_a_soft_time_limit_before_the_worker_budget(
    env: Env, tmp_path: Path
) -> None:
    settings(env, agent_limits_seconds=1800)
    client, _user, thread, _space = await _student(env)
    provider = MeteredProvider(env, lambda request: _answer_now())
    sent = await _post(client, thread, "привет")
    await start_worker(env, provider, tmp_path, budgets={JobKind.CHAT.value: 300.0})
    await env.wait_for(sent.json()["jobId"], {"done"}, within=30)
    async with env.sm() as session:
        limits = await session.scalar(
            select(AgentRunEvent.payload["limits"])
            .where(AgentRunEvent.kind == "run_started")
            .join(AgentRun, AgentRun.run_id == AgentRunEvent.run_id)
            .where(AgentRun.job_id == sent.json()["jobId"])
        )
    assert limits["max_seconds"] == 240.0  # the 300 s budget minus 60 s, below the 1800 s setting


async def _answer_now() -> LlmResponse:
    return answer("Привет!")


# --- stops say why and keep the work ---------------------------------------------------------------


async def test_the_chain_money_cap_stops_and_the_next_message_resumes_without_trace_read(
    env: Env, tmp_path: Path
) -> None:
    settings(env, agent_limits_steps=3, agent_chain_hard_stop_usd=0.5)
    client, _user, thread, _space = await _student(env)
    state = {"answer": False}

    async def respond(request: LlmRequest) -> LlmResponse:
        if state["answer"]:
            return answer("Продолжил с места остановки и закончил.")
        return step_tool(_steps(request), big=True)

    provider = MeteredProvider(env, respond)
    first = await _post(client, thread, "Разбери все источники")
    job_id = first.json()["jobId"]
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(job_id, {"done"}, within=30)

    result = job.result
    assert result is not None
    assert result["stopped"] == "budget"
    assert result["resume"]["resumable"] is True
    assert result["resume"]["reason"] == "money"
    assert result["resume"]["capUsd"] == 0.5
    assert result["resume"]["spentUsd"] >= 0.5
    assert result["cost_usd"] == pytest.approx(await spent(env, job_id), abs=1e-5)
    assert len(provider.requests) == 4  # 3 + the one call the second segment was allowed
    reply = (await agent_texts(env, thread, job_id))[0].blocks[0]["markdown"]
    assert "Остановился: потрачено $0.60 из $0.50 на эту задачу." in reply
    assert "нажмите «Продолжить»" in reply
    rows = await checkpoints_of(env, job_id)
    assert rows[-1].status == "hard_stop"
    assert rows[-1].resumable
    assert result["resume"]["runId"] == rows[-1].run_id

    # the student presses «Продолжить»: the message names the run, the new job starts from it
    state["answer"] = True
    pressed = await _post(client, thread, "Продолжить", resumeRunId=result["resume"]["runId"])
    assert pressed.status_code == 202, pressed.text
    second_id = pressed.json()["jobId"]
    second = await env.wait_for(second_id, {"done"}, within=30)
    assert second.result is not None
    assert second.result["stopped"] == "answer"
    assert second.result["chain"]["resumedFrom"] == "thread"
    assert second.payload["resume_run_id"] == result["resume"]["runId"]

    resumed = provider.requests[-1]
    assert [name for name, _ in tool_calls_of(resumed)] == ["fs_write"] * 4
    roles = [m.role for m in resumed.messages]
    assert roles[-1] == "user"
    assert "Твоя предыдущая работа была прервана" in resumed.messages[-1].content
    assert any(m.role == "user" and m.content == "Продолжить" for m in resumed.messages)
    assert_continues_from_checkpoint(resumed, ["fs_write"] * 4)
    # the new job is a new chain: its money is counted from zero, the old chain is not touched
    assert await spent(env, second_id) < 0.01
    assert (await spent(env, job_id)) >= 0.5


async def test_a_plain_message_resumes_a_stopped_turn_too_and_a_stale_checkpoint_does_not(
    env: Env, tmp_path: Path
) -> None:
    settings(env, agent_limits_steps=3, agent_continuations_max=0)
    client, _user, thread, _space = await _student(env)
    state = {"answer": False}

    async def respond(request: LlmRequest) -> LlmResponse:
        return answer("закончил") if state["answer"] else step_tool(_steps(request))

    provider = MeteredProvider(env, respond)
    first = await _post(client, thread, "Начни работу")
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(first.json()["jobId"], {"done"}, within=30)
    assert job.result is not None
    # CONTINUATIONS_MAX=0 is the manual mode: it stops at the first limit and explains
    assert job.result["resume"]["reason"] == "manual"
    assert job.result["chain"]["segments"] == 1
    reply = (await agent_texts(env, thread, job.id))[0].blocks[0]["markdown"]
    assert "Работа не завершена: достигнут лимит числа шагов." in reply
    assert "Прогресс сохранён" in reply

    state["answer"] = True
    plain = await _post(client, thread, "Продолжай, пожалуйста.")
    second = await env.wait_for(plain.json()["jobId"], {"done"}, within=30)
    assert second.result is not None
    assert second.result["chain"]["resumedFrom"] == "thread"
    assert len(tool_calls_of(provider.requests[-1])) == 3

    # that turn answered: a third message starts from the thread history again
    third = await _post(client, thread, "Спасибо, а что по пятнице?")
    done = await env.wait_for(third.json()["jobId"], {"done"}, within=30)
    assert done.result is not None
    assert done.result["chain"]["resumedFrom"] == "fresh"
    assert tool_calls_of(provider.requests[-1]) == []


async def test_a_checkpoint_older_than_the_window_is_not_resumed(env: Env, tmp_path: Path) -> None:
    settings(env, agent_limits_steps=3, agent_continuations_max=0)
    client, _user, thread, _space = await _student(env)
    state = {"answer": False}

    async def respond(request: LlmRequest) -> LlmResponse:
        return answer("новый ход") if state["answer"] else step_tool(_steps(request))

    provider = MeteredProvider(env, respond)
    first = await _post(client, thread, "Начни работу")
    await start_worker(env, provider, tmp_path)
    await env.wait_for(first.json()["jobId"], {"done"}, within=30)

    state["answer"] = True
    env.advance(hours=25)  # past the 24 h window (the row itself lives for 72 h)
    late = await _post(client, thread, "Продолжай")
    job = await env.wait_for(late.json()["jobId"], {"done"}, within=30)
    assert job.result is not None
    assert job.result["chain"]["resumedFrom"] == "fresh"
    assert tool_calls_of(provider.requests[-1]) == []


async def test_the_number_of_automatic_continuations_is_bounded(env: Env, tmp_path: Path) -> None:
    settings(env, agent_limits_steps=3, agent_continuations_max=1)
    client, _user, thread, _space = await _student(env)
    provider = MeteredProvider(env, lambda request: _tool(request))
    sent = await _post(client, thread, "Работай долго")
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(sent.json()["jobId"], {"done"}, within=30)

    assert job.result is not None
    assert job.result["chain"]["segments"] == 2
    assert job.result["resume"] == {
        "resumable": True,
        "runId": (await checkpoints_of(env, job.id))[-1].run_id,
        "reason": "count",
        "spentUsd": job.result["resume"]["spentUsd"],
        "capUsd": 2.0,
        "continuations": 1,
    }
    reply = (await agent_texts(env, thread, job.id))[0].blocks[0]["markdown"]
    assert "Остановился после 1 автоматического продолжения." in reply


async def _tool(request: LlmRequest) -> LlmResponse:
    return step_tool(_steps(request))


async def test_a_segment_with_no_progress_stops_the_chain(env: Env, tmp_path: Path) -> None:
    settings(env, agent_limits_steps=3)
    client, _user, thread, _space = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:  # the same call again and again
        return tool_response(call("fs_list", {"path": "."}, f"c{_steps(request)}"))

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Что в папке?")
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(sent.json()["jobId"], {"done"}, within=30)
    assert job.result is not None
    assert job.result["resume"]["reason"] == "no_progress"
    assert job.result["chain"]["segments"] == 1
    reply = (await agent_texts(env, thread, job.id))[0].blocks[0]["markdown"]
    assert "последние шаги ничего не дали" in reply


async def test_a_nearly_used_daily_quota_stops_the_chain_before_another_segment(
    env: Env, tmp_path: Path
) -> None:
    settings(env, agent_limits_steps=3, quota_tokens_per_day=1000)
    client, user, thread, _space = await _student(env)
    async with env.sm() as session:
        session.add(
            LlmUsageRow(
                id=new_id(),
                user_id=user,
                job_id=None,
                purpose="agent",
                model="deepseek-flash",
                input_cache_miss=950,
                input_cache_hit=0,
                output=0,
                cost_usd=0,
                latency_ms=0,
                created_at=env.clock.now(),
            )
        )
        await session.commit()
    provider = MeteredProvider(env, lambda request: _tool(request))
    sent = await _post(client, thread, "Работай")
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(sent.json()["jobId"], {"done"}, within=30)
    assert job.result is not None
    assert job.result["resume"]["reason"] == "quota"
    assert job.result["resume"]["resumable"] is True
    assert job.result["chain"]["segments"] == 1
    reply = (await agent_texts(env, thread, job.id))[0].blocks[0]["markdown"]
    assert "в полночь по Москве" in reply


async def test_a_checkpoint_that_cannot_fit_stops_the_chain_and_says_nothing_was_saved(
    env: Env, tmp_path: Path
) -> None:
    settings(env, agent_limits_steps=2, agent_checkpoint_max_bytes=64 * 1024)
    client, _user, thread, _space = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:
        n = _steps(request)
        if n == 0:
            return tool_response(
                call("fs_write", {"path": "a.txt", "content": "x"}, "c0"), content="я" * 100_000
            )
        return step_tool(n)

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Сделай много")
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(sent.json()["jobId"], {"done"}, within=30)
    assert job.result is not None
    assert job.result["resume"]["resumable"] is False
    assert job.result["chain"]["segments"] == 1
    reply = (await agent_texts(env, thread, job.id))[0].blocks[0]["markdown"]
    assert "Прогресс сохранить не удалось" in reply
    assert "нажмите «Продолжить»" not in reply
    assert await checkpoints_of(env, job.id) == []


# --- a transient provider failure pauses and retries --------------------------------------------------


async def test_an_outage_pauses_the_job_then_it_continues_from_the_checkpoint(
    env: Env, tmp_path: Path
) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)
    script: list[str] = ["tool", "503", "answer"]

    async def respond(request: LlmRequest) -> LlmResponse:
        step = script.pop(0)
        if step == "tool":
            return step_tool(0)
        if step == "503":
            raise BotaiError(ErrorCode.LLM_UNAVAILABLE, "Модель временно недоступна")
        return answer("Модель вернулась, закончил.")

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Проверь расписание")
    job_id = sent.json()["jobId"]
    await start_worker(env, provider, tmp_path)

    paused = await wait_queued(env, job_id, "Модель временно недоступна. Попробую снова через 60 с")
    assert "(попытка 1 из 3)" in (paused.stage or "")
    assert paused.run_after == env.clock.now() + timedelta(seconds=60)
    await asyncio.sleep(0.3)
    assert len(provider.requests) == 2  # nothing is called during the pause

    env.advance(seconds=61)
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    assert job.result["stopped"] == "answer"
    assert len(provider.requests) == 3
    resumed = provider.requests[2]
    assert [name for name, _ in tool_calls_of(resumed)] == ["fs_write"]  # from the checkpoint
    assert job.payload["_retry_failures"] == 0  # a pause is not a failure
    assert len(await agent_texts(env, thread, job_id)) == 1


async def test_the_fourth_outage_in_a_row_stops_and_keeps_the_work(
    env: Env, tmp_path: Path
) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:
        if _steps(request) == 0 and len(provider.requests) == 1:
            return step_tool(0)
        raise BotaiError(ErrorCode.LLM_UNAVAILABLE, "Модель временно недоступна")

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Проверь")
    job_id = sent.json()["jobId"]
    await start_worker(env, provider, tmp_path)

    for stage, wait_s in (("через 60 с", 61), ("через 3 мин", 181), ("через 10 мин", 601)):
        await wait_queued(env, job_id, "Модель временно недоступна. Попробую снова " + stage)
        env.advance(seconds=wait_s)
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert job.result is not None
    assert job.result["stopped"] == "error"
    assert job.result["chain"]["segments"] == 4
    assert job.result["resume"]["resumable"] is True
    assert job.result["resume"]["reason"] == "transient"
    assert job.payload["_retry_failures"] == 0  # three pauses are not three failures
    reply = (await agent_texts(env, thread, job_id))[0].blocks[0]["markdown"]
    assert "Модель по-прежнему недоступна после 3 попыток." in reply
    assert "нажмите «Продолжить»" in reply
    assert len(provider.requests) == 5


async def test_a_daily_quota_error_stops_and_the_next_message_resumes_after_the_reset(
    env: Env, tmp_path: Path
) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)
    state = {"quota": True}

    async def respond(request: LlmRequest) -> LlmResponse:
        n = _steps(request)
        if n == 0:
            return step_tool(0)
        if state["quota"]:
            raise BotaiError(
                ErrorCode.QUOTA_EXCEEDED,
                "Суточный лимит модели исчерпан.",
                extra={"scope": "daily", "resetsAt": "2026-10-27T21:00:00Z"},
            )
        return answer("Лимит обновился, закончил.")

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Сделай")
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(sent.json()["jobId"], {"done"}, within=30)
    assert job.result is not None
    assert job.result["resume"]["reason"] == "quota"
    assert job.result["resume"]["resumable"]
    assert job.result["chain"]["segments"] == 1
    assert len(provider.requests) == 2  # no automatic retry of a spent daily quota
    reply = (await agent_texts(env, thread, job.id))[0].blocks[0]["markdown"]
    assert "Суточный лимит модели исчерпан." in reply
    assert "в полночь по Москве" in reply

    state["quota"] = False  # midnight passed
    again = await _post(client, thread, "Продолжай")
    second = await env.wait_for(again.json()["jobId"], {"done"}, within=30)
    assert second.result is not None
    assert second.result["stopped"] == "answer"
    assert [name for name, _ in tool_calls_of(provider.requests[-1])] == ["fs_write"]


async def test_a_per_minute_quota_error_waits_and_goes_on(env: Env, tmp_path: Path) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)
    script = ["tool", "minute", "answer"]

    async def respond(request: LlmRequest) -> LlmResponse:
        step = script.pop(0)
        if step == "tool":
            return step_tool(0)
        if step == "minute":
            resets = (env.clock.now() + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
            raise BotaiError(
                ErrorCode.QUOTA_EXCEEDED,
                "Слишком много запросов к модели подряд.",
                extra={"scope": "minute", "resetsAt": resets},
            )
        return answer("Готово")

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Сделай")
    job_id = sent.json()["jobId"]
    await start_worker(env, provider, tmp_path)
    paused = await wait_queued(env, job_id, "Слишком много запросов к модели. Продолжу через 30 с.")
    assert paused.run_after == env.clock.now() + timedelta(seconds=30)
    env.advance(seconds=31)
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    assert job.result["stopped"] == "answer"


# --- restarts, recovery and cancel ----------------------------------------------------------------------


@pytest.mark.parametrize("how", ["shutdown", "crash"])
async def test_a_restart_in_the_middle_of_a_segment_resumes_from_the_snapshot_not_from_zero(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)
    hold = asyncio.Event()

    async def respond(request: LlmRequest) -> LlmResponse:
        n = _steps(request)
        if n < 3:
            return step_tool(n)
        await hold.wait()
        return answer("Дожал после перезапуска.")

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Долгая работа")
    job_id = sent.json()["jobId"]
    first = await start_worker(env, provider, tmp_path, name="worker-one", shutdown_grace=0.05)

    async def blocked() -> bool:
        return len(provider.requests) == 4

    await wait_until(blocked, what="the fourth model call")
    if how == "crash":
        # a killed process saves nothing at the end: only the periodic snapshots survive
        original = SegmentCheckpointer.save

        async def lose_final(self: SegmentCheckpointer, snapshot: Any) -> None:
            if snapshot.final:
                raise RuntimeError("the process is gone")
            await original(self, snapshot)

        monkeypatch.setattr(SegmentCheckpointer, "save", lose_final)
    first.stop()
    await env.wait_for(job_id, {"queued"}, within=15)
    rows = await checkpoints_of(env, job_id)
    assert len(rows) == 1
    assert rows[0].message_count >= 7
    assert rows[0].status == ("cancelled" if how == "shutdown" else "running")
    assert rows[0].resumable
    monkeypatch.undo()

    if how == "crash":  # and the orphan is found by the next worker's maintenance
        async with env.sm() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    state="running",
                    locked_by="worker-dead",
                    locked_at=env.clock.now() - timedelta(hours=1),
                )
            )
            await session.commit()
    hold.set()
    second = await start_worker(env, provider, tmp_path, name="worker-two")
    if how == "crash":
        assert await second.recover_orphans() == 1
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert job.result is not None
    assert job.result["stopped"] == "answer"
    assert job.attempts == 2
    assert len(provider.requests) == 5  # 4 before the restart, one after: not a second turn
    resumed = provider.requests[4]
    calls = tool_calls_of(resumed)
    assert [name for name, _ in calls] == ["fs_write"] * 3
    assert len({call_id for _, call_id in calls}) == 3  # nothing was done twice
    assert len(await agent_texts(env, thread, job_id)) == 1


async def test_deferrals_do_not_use_up_the_retries_of_a_crash(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)
    hold = asyncio.Event()

    async def respond(request: LlmRequest) -> LlmResponse:
        if len(provider.requests) <= 3:
            raise BotaiError(ErrorCode.LLM_UNAVAILABLE, "Модель временно недоступна")
        await hold.wait()
        return answer("Дошёл до конца.")

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Работа")
    job_id = sent.json()["jobId"]
    first = await start_worker(env, provider, tmp_path, name="worker-one", shutdown_grace=0.05)
    for stage, wait_s in (("через 60 с", 61), ("через 3 мин", 181), ("через 10 мин", 601)):
        await wait_queued(env, job_id, "Модель временно недоступна. Попробую снова " + stage)
        env.advance(seconds=wait_s)

    async def blocked() -> bool:
        return len(provider.requests) == 4

    await wait_until(blocked, what="the fourth segment")
    assert (await env.job(job_id)).attempts == 4  # one claim per segment
    first.stop()
    await env.wait_for(job_id, {"queued"}, within=15)
    async with env.sm() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                state="running",
                locked_by="worker-dead",
                locked_at=env.clock.now() - timedelta(hours=1),
            )
        )
        await session.commit()
    hold.set()
    second = await start_worker(env, provider, tmp_path, name="worker-two")
    # with attempts counted as failures this would be the fourth interruption and the job would die
    assert await second.recover_orphans() == 1
    assert (await env.job(job_id)).state == "queued"
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    assert job.result["stopped"] == "answer"


async def test_cancelling_a_paused_job_works_and_the_work_is_kept(env: Env, tmp_path: Path) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)
    state = {"down": True}

    async def respond(request: LlmRequest) -> LlmResponse:
        n = _steps(request)
        if n == 0:
            return step_tool(0)
        if state["down"]:
            raise BotaiError(ErrorCode.LLM_UNAVAILABLE, "Модель временно недоступна")
        return answer("Вернулся и закончил.")

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Сделай")
    job_id = sent.json()["jobId"]
    await start_worker(env, provider, tmp_path)
    await wait_queued(env, job_id, "Модель временно недоступна")

    stopped = await client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert stopped.status_code == 204
    assert (await env.job(job_id)).state == "cancelled"
    env.advance(seconds=3600)
    await asyncio.sleep(0.3)
    assert (await env.job(job_id)).state == "cancelled"  # the pause ran out, nobody woke it
    assert len(provider.requests) == 2

    # the composer is free and the next message continues from the saved work
    state["down"] = False
    again = await _post(client, thread, "Продолжай")
    assert again.status_code == 202, again.text
    second = await env.wait_for(again.json()["jobId"], {"done"}, within=30)
    assert second.result is not None
    assert second.result["chain"]["resumedFrom"] == "thread"
    assert [name for name, _ in tool_calls_of(provider.requests[-1])] == ["fs_write"]


async def test_cancelling_a_running_segment_keeps_a_resumable_checkpoint(
    env: Env, tmp_path: Path
) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)
    hold = asyncio.Event()
    state = {"hold": True}

    async def respond(request: LlmRequest) -> LlmResponse:
        n = _steps(request)
        if n == 0:
            return step_tool(0)
        if state["hold"]:
            await hold.wait()
        return answer("Закончил после остановки.")

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Сделай")
    job_id = sent.json()["jobId"]
    await start_worker(env, provider, tmp_path)

    async def blocked() -> bool:
        return len(provider.requests) == 2

    await wait_until(blocked, what="the blocked model call")
    stopped = await client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert stopped.status_code == 204
    await env.wait_for(job_id, {"cancelled"}, within=15)

    async def saved() -> bool:
        rows = await checkpoints_of(env, job_id)
        return bool(rows) and rows[-1].status == "cancelled"

    await wait_until(saved, what="the checkpoint of the cancelled run")
    row = (await checkpoints_of(env, job_id))[-1]
    assert row.resumable
    assert row.message_count >= 4

    state["hold"] = False
    again = await _post(client, thread, "Продолжить", resumeRunId=row.run_id)
    assert again.status_code == 202, again.text
    second = await env.wait_for(again.json()["jobId"], {"done"}, within=30)
    assert second.result is not None
    assert second.result["stopped"] == "answer"
    assert [name for name, _ in tool_calls_of(provider.requests[-1])] == ["fs_write"]


# --- the API --------------------------------------------------------------------------------------------


async def test_resume_run_id_is_checked_for_owner_thread_and_freshness(
    env: Env, tmp_path: Path
) -> None:
    settings(env, agent_limits_steps=3, agent_continuations_max=0)
    client, _user, thread, _space = await _student(env)
    other, _other_user, other_thread, _ = await _student(env)
    provider = MeteredProvider(env, lambda request: _tool(request))
    sent = await _post(client, thread, "Начни")
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(sent.json()["jobId"], {"done"}, within=30)
    assert job.result is not None
    run_id = job.result["resume"]["runId"]

    unknown = await _post(client, thread, "Продолжить", resumeRunId=new_id())
    assert unknown.status_code == 409
    assert unknown.json()["code"] == ErrorCode.STALE_TARGET.value
    foreign = await _post(other, other_thread, "Продолжить", resumeRunId=run_id)
    assert foreign.status_code == 409  # somebody else's run
    other_thread_of_owner = await client.post("/api/v1/chat/threads", json={})
    assert other_thread_of_owner.status_code == 201, other_thread_of_owner.text
    wrong_thread = await _post(
        client, other_thread_of_owner.json()["id"], "Продолжить", resumeRunId=run_id
    )
    assert wrong_thread.status_code == 409  # the owner's own run, but of another thread
    assert wrong_thread.json()["code"] == ErrorCode.STALE_TARGET.value
    malformed = await _post(client, thread, "Продолжить", resumeRunId="not-a-ulid")
    assert malformed.status_code == 422

    env.advance(hours=25)
    stale = await _post(client, thread, "Продолжить", resumeRunId=run_id)
    assert stale.status_code == 409
    env.advance(hours=-25)
    ok = await _post(client, thread, "Продолжить", resumeRunId=run_id)
    assert ok.status_code == 202, ok.text
    queued = await env.job(ok.json()["jobId"])
    assert queued.payload["resume_run_id"] == run_id


async def test_the_job_events_carry_the_continuation_stage_for_a_client_that_reconnects(
    env: Env, tmp_path: Path
) -> None:
    settings(env, agent_limits_steps=3, agent_continuations_max=1)
    client, _user, thread, _space = await _student(env)
    provider = MeteredProvider(env, lambda request: _tool(request))
    sent = await _post(client, thread, "Работай")
    job_id = sent.json()["jobId"]
    await start_worker(env, provider, tmp_path)
    await env.wait_for(job_id, {"done"}, within=30)
    async with env.sm() as session:
        thinking = list(
            await session.scalars(
                select(JobEvent.data).where(
                    JobEvent.job_id == job_id,
                    JobEvent.type == "block",
                    JobEvent.data["type"].astext == "thinking",
                    JobEvent.data["stage"].astext.like("Продолжаю автоматически%"),
                )
            )
        )
    assert thinking
    assert thinking[0]["stage"] == "Продолжаю автоматически (1 из 1): достигнут лимит шагов."
    assert thinking[0]["detail"].startswith("Потрачено $")
    assert thinking[0]["state"] == "ok"
    # the checkpoint store is the one the settings describe
    assert checkpoint_store(env.services) is not None
    assert text("1") is not None


# --- review fixes: stale buttons, lost final saves, replays, fresh context ------------------------------


async def test_a_second_press_of_continue_after_the_resumed_turn_answered_is_stale(
    env: Env, tmp_path: Path
) -> None:
    settings(env, agent_limits_steps=3, agent_continuations_max=0)
    client, _user, thread, _space = await _student(env)
    state = {"answer": False}

    async def respond(request: LlmRequest) -> LlmResponse:
        return answer("закончил") if state["answer"] else step_tool(_steps(request))

    provider = MeteredProvider(env, respond)
    first = await _post(client, thread, "Начни работу")
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(first.json()["jobId"], {"done"}, within=30)
    assert job.result is not None
    run_id = job.result["resume"]["runId"]

    state["answer"] = True
    pressed = await _post(client, thread, "Продолжить", resumeRunId=run_id)
    assert pressed.status_code == 202, pressed.text
    second = await env.wait_for(pressed.json()["jobId"], {"done"}, within=30)
    assert second.result is not None
    assert second.result["chain"]["resumedFrom"] == "thread"
    requests_before = len(provider.requests)

    # the old button is pressed again: that turn was taken over and answered, it must not restart
    again = await _post(client, thread, "Продолжить", resumeRunId=run_id)
    assert again.status_code == 409
    assert again.json()["code"] == ErrorCode.STALE_TARGET.value
    assert len(provider.requests) == requests_before
    async with env.sm() as session:
        old = await session.get(AgentRunCheckpoint, run_id)
    assert old is not None
    assert not old.resumable  # retired when the resuming turn saved its own first snapshot


async def test_an_answered_turn_is_not_resumed_when_its_final_save_was_lost(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)
    original = SegmentCheckpointer.save

    async def final_save_fails(self: SegmentCheckpointer, snapshot: Any) -> None:
        if snapshot.final:
            raise RuntimeError("the database blinked")
        await original(self, snapshot)

    monkeypatch.setattr(SegmentCheckpointer, "save", final_save_fails)

    async def respond(request: LlmRequest) -> LlmResponse:
        return step_tool(0) if _steps(request) == 0 else answer("Ответ получен.")

    provider = MeteredProvider(env, respond)
    first = await _post(client, thread, "Сделай и ответь")
    job_id = first.json()["jobId"]
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert job.result is not None
    assert job.result["stopped"] == "answer"
    replies = await agent_texts(env, thread, job_id)
    assert [r.blocks[0]["markdown"] for r in replies] == ["Ответ получен."]
    rows = await checkpoints_of(env, job_id)
    assert rows  # the snapshot of the step reached the database, the final one did not
    assert not any(r.resumable for r in rows)

    followup = await _post(client, thread, "А теперь другой вопрос")
    done = await env.wait_for(followup.json()["jobId"], {"done"}, within=30)
    assert done.result is not None
    assert done.result["chain"]["resumedFrom"] == "fresh"
    assert tool_calls_of(provider.requests[2]) == []  # the follow-up's first request: no old work


async def test_a_job_requeued_after_its_reply_was_stored_does_not_call_the_model(
    env: Env, tmp_path: Path
) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)
    provider = MeteredProvider(env, lambda request: _answer_now())
    first = await _post(client, thread, "привет")
    job_id = first.json()["jobId"]
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    message_id = job.result["messageId"]
    assert len(provider.requests) == 1

    # the worker died after the reply was committed and before the job was marked done
    async with env.sm() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(state="queued", result=None, finished_at=None, locked_by=None, locked_at=None)
        )
        await session.commit()
    again = await env.wait_for(job_id, {"done"}, within=30)

    assert len(provider.requests) == 1  # no second model call
    assert again.result is not None
    assert again.result["messageId"] == message_id
    assert again.result["stopped"] == "answer"
    assert len(await agent_texts(env, thread, job_id)) == 1


async def test_a_job_requeued_after_a_stopped_reply_keeps_its_progress_resumable(
    env: Env, tmp_path: Path
) -> None:
    settings(env, agent_limits_steps=3, agent_continuations_max=0)
    client, _user, thread, _space = await _student(env)
    provider = MeteredProvider(env, lambda request: _tool(request))
    first = await _post(client, thread, "Работай")
    job_id = first.json()["jobId"]
    await start_worker(env, provider, tmp_path)
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    run_id = job.result["resume"]["runId"]
    requests = len(provider.requests)

    async with env.sm() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(state="queued", result=None, finished_at=None, locked_by=None, locked_at=None)
        )
        await session.commit()
    again = await env.wait_for(job_id, {"done"}, within=30)

    assert len(provider.requests) == requests
    assert again.result is not None
    assert again.result["resume"]["resumable"] is True
    assert again.result["resume"]["runId"] == run_id
    assert len(await agent_texts(env, thread, job_id)) == 1


async def test_a_resumed_turn_gets_todays_context_in_front_of_the_new_question(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings(env, agent_limits_steps=3, agent_continuations_max=0, agent_resume_window_hours=72)
    client, _user, thread, _space = await _student(env)
    calls = {"n": 0}

    async def numbered_context(session: Any, user_id: str, thread_row: Any, now: Any) -> Any:
        calls["n"] += 1
        return LlmMessage(role="user", content=f"Контекст приложения №{calls['n']}")

    monkeypatch.setattr(chat_module, "turn_context", numbered_context)
    state = {"answer": False}

    async def respond(request: LlmRequest) -> LlmResponse:
        return answer("закончил") if state["answer"] else step_tool(_steps(request))

    provider = MeteredProvider(env, respond)
    first = await _post(client, thread, "Начни работу")
    await start_worker(env, provider, tmp_path)
    await env.wait_for(first.json()["jobId"], {"done"}, within=30)

    state["answer"] = True
    env.advance(days=2)  # another day: the checkpoint is within the window, its context is not
    second = await _post(client, thread, "Продолжай")
    done = await env.wait_for(second.json()["jobId"], {"done"}, within=30)
    assert done.result is not None
    assert done.result["chain"]["resumedFrom"] == "thread"

    request = provider.requests[-1]
    contexts = [
        (index, m.content)
        for index, m in enumerate(request.messages)
        if m.content.startswith("Контекст приложения")
    ]
    assert [text for _, text in contexts] == ["Контекст приложения №1", "Контекст приложения №2"]
    last_tool = max(i for i, m in enumerate(request.messages) if m.role == "tool")
    question = next(i for i, m in enumerate(request.messages) if m.content == "Продолжай")
    assert last_tool < contexts[1][0] < question  # today's context sits right before the question


async def test_a_message_while_the_turn_waits_for_the_model_is_busy(
    env: Env, tmp_path: Path
) -> None:
    settings(env)
    client, _user, thread, _space = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:
        raise BotaiError(ErrorCode.LLM_UNAVAILABLE, "Модель временно недоступна")

    provider = MeteredProvider(env, respond)
    sent = await _post(client, thread, "Сделай")
    job_id = sent.json()["jobId"]
    await start_worker(env, provider, tmp_path)
    waiting = await wait_queued(env, job_id, "Модель временно недоступна")
    assert waiting.run_after > env.clock.now()  # deferred, not claimable yet

    busy = await _post(client, thread, "Ещё вопрос")
    assert busy.status_code == 409
    assert busy.json()["code"] == ErrorCode.AGENT_BUSY.value
