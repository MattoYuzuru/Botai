"""GET /jobs/{id}/events (contracts/events.md §1): replay, Last-Event-ID, live follow, keepalive."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select

from botai.contracts.agent_ui import ChoiceBlock, ChoiceOption, InputBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services
from botai.db.models import JobAnswer, JobEvent
from botai.jobs import sse
from botai.jobs.events import MAX_BYTES, MAX_EVENTS, append_event
from tests.auth.support import NOW
from tests.jobs.env import Env, live_http, session_header

pytestmark = pytest.mark.db

Payload = dict[str, Any]
SECRET = "S3cr3t-FIXTURE-0b7e"


def parse(body: str) -> tuple[list[dict[str, Any]], list[str]]:
    """SSE text to (events, comments)."""
    events: list[dict[str, Any]] = []
    comments: list[str] = []
    current: dict[str, Any] = {}
    for line in body.replace("\r\n", "\n").split("\n"):
        if not line:
            if current:
                events.append(current)
                current = {}
        elif line.startswith(":"):
            comments.append(line[1:].strip())
        else:
            field, _, value = line.partition(":")
            value = value.lstrip(" ")
            current[field] = json.loads(value) if field == "data" else value
    return events, comments


async def _finished_job(env: Env) -> tuple[httpx.AsyncClient, str, str]:
    """A done CONNECT job with a status, a block and a result."""

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        await ctx.status("Ищу расписание")
        await ctx.emit(
            ChoiceBlock(
                id=new_id(),
                at=NOW,
                question="Что подключить?",
                mode="single",
                required=False,
                options=[ChoiceOption(value="a", label="А"), ChoiceOption(value="b", label="Б")],
            )
        )
        return {"found": 2}

    client = env.new_client()
    acc = await env.account(client)
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(JobKind.CONNECT, user_id=acc.id, connection_id=conn)
    env.start(env.worker({JobKind.CONNECT: handler}))
    await env.wait_for(job_id, {"done"})
    return client, acc.id, job_id


async def test_a_finished_job_is_replayed_in_order_and_the_stream_ends(env: Env) -> None:
    client, _, job_id = await _finished_job(env)
    r = await client.get(f"/api/v1/jobs/{job_id}/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events, _ = parse(r.text)
    assert [e["id"] for e in events] == [str(n) for n in range(1, len(events) + 1)]
    assert [e["event"] for e in events] == ["status", "status", "status", "block", "status", "done"]
    assert events[0]["data"] == {
        "state": "queued",
        "stage": None,
        "startedAt": None,
        "elapsedMs": 0,
    }
    assert events[2]["data"]["stage"] == "Ищу расписание"
    assert events[2]["data"]["state"] == "running"
    assert events[2]["data"]["startedAt"].endswith("Z")
    assert events[3]["data"]["type"] == "choice"
    assert events[3]["data"]["options"][0] == {"value": "a", "label": "А", "hint": None}
    assert events[-1]["data"] == {"result": {"found": 2}}


async def test_last_event_id_resumes_after_the_last_seen_event(env: Env) -> None:
    client, _, job_id = await _finished_job(env)
    everything, _ = parse((await client.get(f"/api/v1/jobs/{job_id}/events")).text)
    r = await client.get(f"/api/v1/jobs/{job_id}/events", headers={"Last-Event-ID": "3"})
    events, _ = parse(r.text)
    assert [e["id"] for e in events] == [e["id"] for e in everything if int(e["id"]) > 3]
    assert events[0]["event"] == "block"

    last = everything[-1]["id"]
    caught_up = await client.get(f"/api/v1/jobs/{job_id}/events", headers={"Last-Event-ID": last})
    # nothing is repeated; 204 makes a browser's EventSource stop reconnecting to a closed stream
    assert caught_up.status_code == 204
    assert caught_up.content == b""
    junk = await client.get(
        f"/api/v1/jobs/{job_id}/events", headers={"Last-Event-ID": "not-a-number"}
    )
    assert len(parse(junk.text)[0]) == len(everything)


async def test_only_the_owner_may_listen(env: Env) -> None:
    owner, _, job_id = await _finished_job(env)
    stranger = env.new_client()
    await env.account(stranger, email="stranger@example.com")
    r = await stranger.get(f"/api/v1/jobs/{job_id}/events")
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
    assert (await env.new_client().get(f"/api/v1/jobs/{job_id}/events")).status_code == 401
    assert (await owner.get(f"/api/v1/jobs/{new_id()}/events")).status_code == 404


async def test_a_job_whose_buffer_expired_still_tells_how_it_ended(env: Env) -> None:
    client, _, job_id = await _finished_job(env)
    async with env.sm() as s:
        await s.execute(delete(JobEvent).where(JobEvent.job_id == job_id))
        await s.commit()
    events, _ = parse((await client.get(f"/api/v1/jobs/{job_id}/events")).text)
    assert [e["event"] for e in events] == ["status", "done"]
    assert events[0]["data"]["state"] == "done"
    assert events[1]["data"] == {"result": {"found": 2}}


async def test_a_failed_job_with_an_expired_buffer_replays_its_error(env: Env) -> None:
    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        raise BotaiError(ErrorCode.LOGIN_FAILED, "Вуз не принял пароль")

    client = env.new_client()
    acc = await env.account(client)
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(JobKind.CONNECT, user_id=acc.id, connection_id=conn)
    env.start(env.worker({JobKind.CONNECT: handler}))
    await env.wait_for(job_id, {"failed"})
    live, _ = parse((await client.get(f"/api/v1/jobs/{job_id}/events")).text)
    assert live[-1]["event"] == "error"
    assert live[-1]["data"]["code"] == "login_failed"
    async with env.sm() as s:
        await s.execute(delete(JobEvent).where(JobEvent.job_id == job_id))
        await s.commit()
    late, _ = parse((await client.get(f"/api/v1/jobs/{job_id}/events")).text)
    assert [e["event"] for e in late] == ["status", "error"]
    assert late[-1]["data"]["detail"] == "Вуз не принял пароль"
    assert late[-1]["data"]["jobId"] == job_id


# --- following a running job on a real socket --------------------------------------------------------


class Stream:
    """A live SSE connection that collects events and comments as they arrive."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.events: list[dict[str, Any]] = []
        self.comments: list[str] = []
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        current: dict[str, Any] = {}
        async for line in self.response.aiter_lines():
            if not line:
                if current:
                    self.events.append(current)
                    current = {}
            elif line.startswith(":"):
                self.comments.append(line[1:].strip())
            else:
                field, _, value = line.partition(":")
                value = value.lstrip(" ")
                current[field] = json.loads(value) if field == "data" else value

    async def until(self, predicate: Callable[[Stream], bool], *, within: float = 10.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + within
        while not predicate(self):
            if loop.time() > deadline:
                raise AssertionError(f"timed out; got {[e['event'] for e in self.events]}")
            await asyncio.sleep(0.02)

    async def finished(self) -> None:
        await asyncio.wait_for(self._task, 10)

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]


@contextlib.asynccontextmanager
async def listen(
    http: httpx.AsyncClient, path: str, headers: dict[str, str]
) -> AsyncIterator[Stream]:
    async with http.stream("GET", path, headers=headers) as response:
        yield Stream(response)


async def test_a_live_stream_follows_the_job_and_ends_with_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sse, "KEEPALIVE_SECONDS", 0.2)
    step = asyncio.Event()

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        await ctx.status("Первый этап")
        await step.wait()
        await ctx.status("Второй этап")
        return {"ok": True}

    client = env.new_client()
    acc = await env.account(client)
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(JobKind.CONNECT, user_id=acc.id, connection_id=conn)
    env.start(env.worker({JobKind.CONNECT: handler}))

    async with (
        live_http(env.app) as http,
        listen(http, f"/api/v1/jobs/{job_id}/events", session_header(client)) as stream,
    ):
        assert stream.response.status_code == 200
        await stream.until(lambda s: any(e["data"].get("stage") == "Первый этап" for e in s.events))
        await stream.until(lambda s: len(s.comments) >= 2)  # idle: the keepalive keeps coming
        assert set(stream.comments) == {"keepalive"}
        assert "done" not in stream.names()

        step.set()
        await stream.finished()  # the job ended, so did the stream
        assert stream.names()[-1] == "done"
        assert stream.events[-1]["data"] == {"result": {"ok": True}}
        stages = [e["data"]["stage"] for e in stream.events if e["event"] == "status"]
        assert "Второй этап" in stages
        assert [int(e["id"]) for e in stream.events] == sorted(int(e["id"]) for e in stream.events)


async def test_events_missed_while_disconnected_are_replayed_on_reconnect(env: Env) -> None:
    go = asyncio.Event()

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        await ctx.status("этап 1")
        await go.wait()
        await ctx.status("этап 2")
        await ctx.status("этап 3")
        await go.wait()
        return {}

    client = env.new_client()
    acc = await env.account(client)
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(JobKind.CONNECT, user_id=acc.id, connection_id=conn)
    env.start(env.worker({JobKind.CONNECT: handler}))

    async with live_http(env.app) as http:
        headers = session_header(client)
        async with http.stream("GET", f"/api/v1/jobs/{job_id}/events", headers=headers) as resp:
            stream = Stream(resp)
            await stream.until(lambda s: any(e["data"].get("stage") == "этап 1" for e in s.events))
            seen = int(stream.events[-1]["id"])
            stream._task.cancel()  # the phone loses signal ...
        go.set()  # ... and the job moves on without anyone listening
        await env.wait_for(job_id, {"running"})
        for _ in range(200):
            stages = [e.data.get("stage") for e in await env.events(job_id) if e.type == "status"]
            if "этап 3" in stages:
                break
            await asyncio.sleep(0.02)

        async with http.stream(
            "GET", f"/api/v1/jobs/{job_id}/events", headers={**headers, "Last-Event-ID": str(seen)}
        ) as resp:
            again = Stream(resp)
            await again.until(lambda s: any(e["data"].get("stage") == "этап 3" for e in s.events))
            assert [e["data"]["stage"] for e in again.events if e["event"] == "status"][:2] == [
                "этап 2",
                "этап 3",
            ]
            assert int(again.events[0]["id"]) == seen + 1
            go.set()
            await again.finished()
            assert again.names()[-1] == "done"


async def test_the_whole_conversation_streams_without_the_secret(env: Env) -> None:
    password = InputBlock(
        id=new_id(),
        at=NOW,
        question="Пароль",
        input_kind="password",
        required=True,
        secret=True,
        autocomplete="current-password",
    )

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        secret = await ctx.ask_secret(password)
        return {"length": len(secret.get_secret_value())}

    client = env.new_client()
    acc = await env.account(client)
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(JobKind.CONNECT, user_id=acc.id, connection_id=conn)
    env.start(env.worker({JobKind.CONNECT: handler}))

    async with (
        live_http(env.app) as http,
        listen(http, f"/api/v1/jobs/{job_id}/events", session_header(client)) as stream,
    ):
        await stream.until(
            lambda s: "block" in s.names() and s.events[-1]["data"].get("state") == "waiting_user"
        )
        block = next(e for e in stream.events if e["event"] == "block")
        assert block["data"]["secret"] is True
        r = await http.post(
            f"/api/v1/jobs/{job_id}/answers",
            headers=session_header(client),
            json={"blockId": block["data"]["id"], "values": SECRET},
        )
        assert r.status_code == 204
        await stream.finished()
    assert stream.names()[-1] == "done"
    assert SECRET not in json.dumps(stream.events)
    assert stream.events[-1]["data"] == {"result": {"length": len(SECRET)}}


# --- the buffer -----------------------------------------------------------------------------------


async def test_the_buffer_keeps_the_newest_500_events(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn)
    async with env.sm() as s:
        for n in range(MAX_EVENTS + 40):
            await append_event(s, job_id, "status", {"state": "running", "stage": f"шаг {n}"})
        await s.commit()
    rows = await env.events(job_id)
    assert len(rows) == MAX_EVENTS
    assert rows[-1].data["stage"] == f"шаг {MAX_EVENTS + 39}"
    seqs = [r.seq for r in rows]
    assert seqs == sorted(seqs)
    assert seqs[-1] == MAX_EVENTS + 41  # the queued event was seq 1; seq never restarts


async def test_the_buffer_is_bounded_in_bytes_too(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn)
    chunk = "я" * 100_000  # ~200 KB per event
    async with env.sm() as s:
        for n in range(15):
            await append_event(s, job_id, "block", {"id": str(n), "text": chunk})
        await s.commit()
        total = await s.scalar(
            select(func.sum(func.pg_column_size(JobEvent.data))).where(JobEvent.job_id == job_id)
        )
    assert total is not None
    assert total <= MAX_BYTES
    rows = await env.events(job_id)
    assert rows[-1].data["id"] == "14"


async def test_an_oversized_event_is_refused(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn)
    async with env.sm() as s:
        with pytest.raises(ValueError, match="over the"):
            await append_event(s, job_id, "block", {"text": "x" * 300_000})


async def test_events_and_answers_are_dropped_ten_minutes_after_the_job_ends(env: Env) -> None:
    client, uid, job_id = await _finished_job(env)
    async with env.sm() as s:
        s.add(JobAnswer(job_id=job_id, block_id=new_id(), values="x", is_secret=False))
        await s.commit()
    worker = env.worker({})
    await worker.maintain()
    assert await env.events(job_id)  # just finished: still replayable
    assert await env.answers(job_id)

    env.advance(minutes=9)
    await worker.maintain()
    assert await env.events(job_id)

    env.advance(minutes=2)
    await worker.maintain()
    assert not await env.events(job_id)
    assert not await env.answers(job_id)
    job = await env.job(job_id)  # the job and its result stay
    assert job.state == "done"
    assert job.result == {"found": 2}
    r = await client.get(f"/api/v1/jobs/{job_id}")
    assert r.json()["result"] == {"found": 2}
    assert uid
