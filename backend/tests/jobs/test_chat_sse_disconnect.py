"""A closed tab or a lost connection never cancels a CHAT-v4 job (D-040, proven end to end).

The stream (`GET /jobs/{id}/events`) only reads `job_events`; the only thing that stops a job is
`POST /jobs/{id}/cancel`. These tests open a real SSE socket on a real worker running the real v4
chat handler, drop the connection hard (the client is closed, not politely finished), let the worker
heartbeat as it does in production, and check that the job was neither cancelled nor released, that
it finishes with exactly one reply, and that the events the client missed replay with
`Last-Event-ID`. The negative control is the same scenario ended by an explicit cancel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from botai.agent.v4.chat import make_chat_handlers
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import LlmRequest, LlmResponse
from botai.db.models import AgentRun, ChatMessage
from botai.jobs import sse
from botai.jobs.runner import Worker
from tests.agent.v4.runtime_support import answer
from tests.jobs.env import Env, live_server, session_header
from tests.jobs.test_agent_chat_continuation import step_tool, wait_queued
from tests.jobs.test_agent_chat_v4 import FlowProvider, _post, _steps, _student
from tests.jobs.test_sse import Stream

pytestmark = pytest.mark.db


class Turn:
    """A CHAT-v4 job in flight on a worker, with its provider blocked until `gate` is set."""

    def __init__(
        self,
        env: Env,
        client: httpx.AsyncClient,
        thread: str,
        job_id: str,
        worker: Worker,
        provider: FlowProvider,
        gate: asyncio.Event,
        cancels: list[str],
    ) -> None:
        self.cancels = cancels
        self.env = env
        self.client = client
        self.thread = thread
        self.job_id = job_id
        self.worker = worker
        self.provider = provider
        self.gate = gate


async def start_turn(
    env: Env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[LlmRequest, asyncio.Event], Awaitable[LlmResponse]] | None = None,
) -> Turn:
    env.settings.agent_engine = "v4"
    env.settings.agent_checkpoint_every_steps = 1
    monkeypatch.setattr(sse, "KEEPALIVE_SECONDS", 0.1)  # a dead socket is noticed at the next ping
    client, _user, thread, _space = await _student(env)
    gate = asyncio.Event()

    async def default(request: LlmRequest, gate: asyncio.Event) -> LlmResponse:
        if _steps(request) == 0:
            return step_tool(0)
        await gate.wait()  # the model is "thinking" while the phone goes away
        return answer("Готово: ответ дождался возвращения.")

    handler = respond or default

    async def call(request: LlmRequest) -> LlmResponse:
        return await handler(request, gate)

    provider = FlowProvider(call)
    sent = await _post(client, thread, "Собери расписание")
    assert sent.status_code == 202, sent.text
    # every way the worker can be told to stop a job (NOTIFY and heartbeat) goes through this method
    cancels: list[str] = []
    original = Worker._on_cancel  # pyright: ignore[reportPrivateUsage]

    def spy(self: Worker, job_id: str, reason: str = "user") -> None:
        cancels.append(job_id)
        original(self, job_id, reason)

    monkeypatch.setattr(Worker, "_on_cancel", spy)
    worker = env.worker(
        make_chat_handlers(
            provider=provider, artifact_root=str(tmp_path / "artifacts"), with_browser=False
        )
    )
    env.start(worker)
    return Turn(env, client, thread, sent.json()["jobId"], worker, provider, gate, cancels)


async def requests_made(turn: Turn, count: int) -> None:
    for _ in range(500):
        if len(turn.provider.requests) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"the provider got {len(turn.provider.requests)} requests, wanted {count}")


async def hard_drop(base: str, turn: Turn) -> int:
    """Open the event stream, read at least one event, then kill the connection. Returns last id."""
    http = httpx.AsyncClient(base_url=base, timeout=30)
    response_cm = http.stream(
        "GET", f"/api/v1/jobs/{turn.job_id}/events", headers=session_header(turn.client)
    )
    response = await response_cm.__aenter__()
    stream = Stream(response)
    await stream.until(lambda s: len(s.events) >= 1)
    seen = int(stream.events[-1]["id"])
    stream._task.cancel()  # pyright: ignore[reportPrivateUsage]
    # no polite `__aexit__` of the stream: close the whole client under it, like a closed tab
    await http.aclose()
    return seen


async def settle(turn: Turn) -> list[str]:
    """Let the server notice the dead socket and the worker heartbeat; return the cancels it saw."""
    for _ in range(3):  # three heartbeat periods of a production worker (15 s each)
        await asyncio.sleep(0.25)
        await turn.worker.heartbeat()
    return turn.cancels


async def events_after(base: str, turn: Turn, seen: int) -> Stream:
    headers = {**session_header(turn.client), "Last-Event-ID": str(seen)}
    async with (
        httpx.AsyncClient(base_url=base, timeout=30) as http,
        http.stream("GET", f"/api/v1/jobs/{turn.job_id}/events", headers=headers) as response,
    ):
        stream = Stream(response)
        await stream.finished()
    return stream


async def one_reply(env: Env, turn: Turn) -> list[ChatMessage]:
    async with env.sm() as session:
        rows = list(
            await session.scalars(
                select(ChatMessage).where(
                    ChatMessage.thread_id == turn.thread,
                    ChatMessage.job_id == turn.job_id,
                    ChatMessage.role == "agent",
                )
            )
        )
    return [row for row in rows if row.blocks and row.blocks[0].get("type") == "text"]


async def test_a_dropped_sse_connection_does_not_cancel_a_running_chat_job(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = await start_turn(env, tmp_path, monkeypatch)
    async with live_server(env.app) as base:
        await requests_made(turn, 2)  # the second model call is parked on the gate
        seen = await hard_drop(base, turn)

        cancels = await settle(turn)
        job = await env.job(turn.job_id)
        assert job.state == "running"
        assert job.locked_by == turn.worker.name  # still claimed, not released
        assert cancels == []
        assert turn.worker._cancel_reason == {}  # pyright: ignore[reportPrivateUsage]
        assert len(turn.provider.requests) == 2  # the model call is still in flight, not restarted

        turn.gate.set()
        done = await env.wait_for(turn.job_id, {"done"}, within=30)
        assert done.result is not None
        assert done.result["stopped"] == "answer"
        assert done.result["chain"]["segments"] == 1
        replies = await one_reply(env, turn)
        assert len(replies) == 1
        assert replies[0].blocks[0]["markdown"] == "Готово: ответ дождался возвращения."
        async with env.sm() as session:
            statuses = list(
                await session.scalars(select(AgentRun.status).where(AgentRun.job_id == turn.job_id))
            )
        assert statuses == ["answer"]

        replay = await events_after(base, turn, seen)
        assert int(replay.events[0]["id"]) == seen + 1  # nothing lost, nothing repeated
        assert replay.names()[-1] == "done"
        assert replay.events[-1]["data"]["result"]["messageId"] == replies[0].id
        blocks = [e["data"] for e in replay.events if e["event"] == "block"]
        assert any(b.get("type") == "text" for b in blocks)


async def test_a_dropped_connection_between_segments_does_not_stop_the_chain(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = ["tool", "503", "answer"]

    async def respond(request: LlmRequest, gate: asyncio.Event) -> LlmResponse:
        step = script.pop(0)
        if step == "tool":
            return step_tool(0)
        if step == "503":
            raise BotaiError(ErrorCode.LLM_UNAVAILABLE, "Модель временно недоступна")
        return answer("Цепочка дошла до конца без зрителя.")

    turn = await start_turn(env, tmp_path, monkeypatch, respond)
    async with live_server(env.app) as base:
        paused = await wait_queued(env, turn.job_id, "Модель временно недоступна")
        assert paused.run_after is not None
        seen = await hard_drop(base, turn)  # the phone goes away while the job waits to retry

        cancels = await settle(turn)
        assert cancels == []
        assert (await env.job(turn.job_id)).state == "queued"  # still waiting, not cancelled
        env.advance(seconds=61)
        done = await env.wait_for(turn.job_id, {"done"}, within=30)

        assert done.result is not None
        assert done.result["chain"]["segments"] == 2
        assert len(await one_reply(env, turn)) == 1
        replay = await events_after(base, turn, seen)
        assert replay.names()[-1] == "done"
        assert int(replay.events[0]["id"]) == seen + 1


async def test_negative_control_an_explicit_cancel_does_stop_the_same_job(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = await start_turn(env, tmp_path, monkeypatch)
    async with live_server(env.app) as base:
        await requests_made(turn, 2)
        await hard_drop(base, turn)
        cancels = await settle(turn)
        assert cancels == []
        assert (await env.job(turn.job_id)).state == "running"

        stopped = await turn.client.post(f"/api/v1/jobs/{turn.job_id}/cancel")
        assert stopped.status_code == 204
        job = await env.wait_for(turn.job_id, {"cancelled"}, within=15)
        assert job.state == "cancelled"
        for _ in range(200):
            if cancels:
                break
            await asyncio.sleep(0.02)
        assert cancels == [turn.job_id]  # the worker was told, by the cancel and by nothing else
        assert await one_reply(env, turn) == []
        turn.gate.set()


async def test_a_paused_job_without_a_viewer_waits_for_its_time_and_finishes(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody ever listens: the paused job waits for its time, not for a viewer."""
    script = ["503", "answer"]

    async def respond(request: LlmRequest, gate: asyncio.Event) -> LlmResponse:
        step = script.pop(0)
        if step == "503":
            raise BotaiError(ErrorCode.LLM_UNAVAILABLE, "Модель временно недоступна")
        return answer("Дождался.")

    turn = await start_turn(env, tmp_path, monkeypatch, respond)
    paused = await wait_queued(env, turn.job_id, "Модель временно недоступна")
    assert paused.run_after is not None
    await settle(turn)
    assert (await env.job(turn.job_id)).state == "queued"
    env.advance(seconds=61)
    done = await env.wait_for(turn.job_id, {"done"}, within=30)
    assert done.finished_at is not None
    assert timedelta(0) <= env.clock.now() - paused.run_after
    reply: Any = (await one_reply(env, turn))[0].blocks[0]
    assert reply["markdown"] == "Дождался."
