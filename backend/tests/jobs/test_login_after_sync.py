"""A sync that ended because the student must log in again asks for it in the thread of the source,
and does not take a worker: a login window waits for a person for up to half an hour (phase 2)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from botai.core.handlers import HandlerTable
from botai.core.ids import new_id
from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services
from botai.db.models import ChatMessage, ChatThread, Connection, Job
from botai.jobs.runner import Claimed
from botai.spaces.conversations import LOGIN_NEEDED
from tests.jobs.env import Env

pytestmark = pytest.mark.db

Payload = dict[str, Any]


async def hold_login(ctx: JobContext, services: Services, payload: Payload) -> Payload:
    await asyncio.Event().wait()  # a login waits for the student for as long as the test looks
    return {}


async def answer_chat(ctx: JobContext, services: Services, payload: Payload) -> Payload:
    return {"answered": True}


def handlers(result: Payload) -> HandlerTable:
    async def sync(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        return result

    return {JobKind.SYNC: sync, JobKind.CONNECT: hold_login, JobKind.CHAT: answer_chat}


async def source(
    env: Env,
    *,
    status: str = "needs_reauth",
    adapter: str | None = "recipe",
    client: httpx.AsyncClient | None = None,
    with_thread: bool = False,
) -> tuple[str, str, str | None]:
    """(user, connection, thread): a source that was connected in `thread` when `with_thread`."""
    acc = await env.account(client)
    _, conn = await env.connection(acc.id, status=status)
    thread: str | None = None
    async with env.sm() as session:
        await session.execute(
            update(Connection).where(Connection.id == conn).values(adapter=adapter)
        )
        if with_thread:
            row = ChatThread(user_id=acc.id, title="Подключение")
            session.add(row)
            await session.flush()
            thread = row.id
            session.add(
                Job(
                    id=new_id(),
                    user_id=acc.id,
                    kind="connect",
                    state="done",
                    connection_id=conn,
                    thread_id=thread,
                    run_after=env.clock.now(),
                    payload={},
                )
            )
        await session.commit()
    return acc.id, conn, thread


async def sync_once(env: Env, user: str, conn: str) -> str:
    job_id = await env.services.queue.enqueue(
        JobKind.SYNC, user_id=user, connection_id=conn, dedupe=False
    )
    await env.wait_for(job_id, {"done", "failed"})
    return job_id


async def prompts(env: Env, user: str, *, count: int | None = None) -> list[ChatMessage]:
    """The messages that ask for a login. The runner writes one right after the sync is marked
    done, so a test that expects `count` of them waits for it; none is checked after a pause."""
    for _ in range(150):
        async with env.sm() as session:
            found = [
                m
                for m in await session.scalars(
                    select(ChatMessage).where(
                        ChatMessage.user_id == user, ChatMessage.role == "agent"
                    )
                )
                if str(m.blocks[0].get("markdown", "")).startswith(LOGIN_NEEDED)
            ]
        if count is None or len(found) >= count:
            return found
        await asyncio.sleep(0.02)
    return found


async def logins(env: Env, user: str | None = None) -> list[Job]:
    async with env.sm() as session:
        query = select(Job).where(Job.kind == JobKind.CONNECT.value)
        if user is not None:
            query = query.where(Job.user_id == user)
        return list(await session.scalars(query))


async def test_a_sync_that_needs_a_login_asks_for_it_in_the_source_thread_and_starts_nothing(
    env: Env,
) -> None:
    client = env.new_client()
    user, conn, thread = await source(env, client=client, with_thread=True)
    env.start(env.worker(handlers({"status": "needs_reauth"}), shutdown_grace=0.2))
    await sync_once(env, user, conn)
    (ask,) = await prompts(env, user, count=1)
    assert ask.thread_id == thread  # the thread the source was connected in
    block = ask.blocks[0]
    assert block["type"] == "text"
    assert f"botai://connections/{conn}" in block["markdown"]
    assert "только в нём, не в чат" in block["markdown"]  # no password is asked for here
    (reply,) = block["quickReplies"]
    assert reply.startswith("Войди снова в «")
    assert len(reply) <= 60
    # nothing was started: no browser task, no worker slot taken
    assert [j for j in await logins(env, user) if j.state != "done"] == []
    # the student sees it: an unread message and a thread that waits for them
    listed = {t["id"]: t for t in (await client.get("/api/v1/chat/threads")).json()}
    assert listed[thread or ""]["unread"] is True
    assert listed[thread or ""]["attention"] == "waiting_user"


async def test_the_source_without_a_thread_gets_one_for_the_request(env: Env) -> None:
    user, conn, _ = await source(env)
    env.start(env.worker(handlers({"status": "needs_reauth"}), shutdown_grace=0.2))
    await sync_once(env, user, conn)
    (ask,) = await prompts(env, user, count=1)
    async with env.sm() as session:
        thread = await session.get(ChatThread, ask.thread_id)
    assert thread is not None
    assert thread.user_id == user
    assert await logins(env, user) == []


async def test_it_asks_once_per_expiry_and_again_after_a_good_sync(env: Env) -> None:
    user, conn, _ = await source(env, with_thread=True)
    worker = env.worker(handlers({"status": "needs_reauth"}), shutdown_grace=0.2)
    ended = Claimed(id="x", user_id=user, kind="sync", connection_id=conn, payload={}, attempts=1)
    await worker._ask_for_login_after(ended, {"status": "needs_reauth"})
    await worker._ask_for_login_after(ended, {"status": "needs_reauth"})
    await asyncio.sleep(0.2)
    assert len(await prompts(env, user)) == 1
    # the student logged in, a sync went well, the login expired again
    (first,) = await prompts(env, user)
    async with env.sm() as session:
        await session.execute(
            update(Connection)
            .where(Connection.id == conn)
            .values(last_sync_at=first.created_at + timedelta(seconds=1))
        )
        await session.commit()
    await worker._ask_for_login_after(ended, {"status": "needs_reauth"})
    asked = await prompts(env, user, count=2)
    assert len(asked) == 2
    assert {m.thread_id for m in asked} == {first.thread_id}


@pytest.mark.parametrize(
    ("result", "status", "adapter"),
    [
        ({"status": "ok"}, "needs_reauth", "recipe"),
        ({"status": "needs_reauth"}, "active", "recipe"),  # the login was repaired meanwhile
        ({"status": "needs_reauth"}, "needs_reauth", None),  # not a login through the browser
        ({"status": "needs_reauth"}, "needs_reauth", "ics"),
        ({"status": "needs_reauth"}, "disabled", "recipe"),
    ],
)
async def test_nothing_is_asked_when_it_is_not_the_students_to_do(
    env: Env, result: Payload, status: str, adapter: str | None
) -> None:
    user, conn, _ = await source(env, status=status, adapter=adapter)
    env.start(env.worker(handlers(result), shutdown_grace=0.2))
    await sync_once(env, user, conn)
    await asyncio.sleep(0.3)
    assert await prompts(env, user) == []
    assert await logins(env, user) == []


async def test_many_sources_expiring_at_once_take_no_worker_and_chat_goes_through(
    env: Env,
) -> None:
    sources = [await source(env) for _ in range(8)]
    env.start(env.worker(handlers({"status": "needs_reauth"}), concurrency=4, shutdown_grace=0.2))
    syncs = [
        await env.services.queue.enqueue(
            JobKind.SYNC, user_id=user, connection_id=conn, dedupe=False
        )
        for user, conn, _ in sources
    ]
    for job_id in syncs:
        await env.wait_for(job_id, {"done"})
    for user, _conn, _ in sources:
        assert len(await prompts(env, user, count=1)) == 1
    # eight students are asked, and no worker is held by any of them
    assert await logins(env) == []
    user, _conn, thread = await source(env, with_thread=True)
    chat = await env.services.queue.enqueue(
        JobKind.CHAT, user_id=user, thread_id=thread, payload={}, dedupe=False
    )
    assert (await env.wait_for(chat, {"done"}, within=3)).state == "done"


async def test_no_more_than_two_logins_wait_for_people_at_once(env: Env) -> None:
    """The student's own «Войди снова» can fill every place if the queue is left alone; the cap
    keeps the other places for work that does not wait for a person."""
    running: set[str] = set()
    release: dict[str, asyncio.Event] = {}

    async def login(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        running.add(ctx.job_id)
        try:
            await release.setdefault(ctx.job_id, asyncio.Event()).wait()
        finally:
            running.discard(ctx.job_id)
        return {}

    table = handlers({"status": "ok"})
    table[JobKind.CONNECT] = login
    env.start(env.worker(table, concurrency=4, shutdown_grace=0.2))
    jobs: list[str] = []
    for _ in range(5):
        user, conn, _ = await source(env, status="active")
        jobs.append(
            await env.services.queue.enqueue(
                JobKind.CONNECT,
                user_id=user,
                connection_id=conn,
                payload={"manual_login": True},
                dedupe=False,
            )
        )
    for _ in range(200):
        if len(running) == 2:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.4)  # long enough for a third one to start if it could
    assert len(running) == 2
    other, other_conn, _ = await source(env, status="active")
    sync = await env.services.queue.enqueue(
        JobKind.SYNC, user_id=other, connection_id=other_conn, dedupe=False
    )
    assert (await env.wait_for(sync, {"done"}, within=3)).state == "done"  # a place was left
    first = sorted(running)[0]
    release[first].set()  # one login ends: the next one takes the place
    for _ in range(200):
        if len(running) == 2 and first not in running:
            break
        await asyncio.sleep(0.02)
    assert len(running) == 2
    assert first not in running

    async def all_done() -> bool:
        return all([(await env.job(job_id)).state == "done" for job_id in jobs])

    for _ in range(500):  # the rest: each one that starts is let go, one after the other
        for event in list(release.values()):
            event.set()
        if await all_done():
            break
        await asyncio.sleep(0.02)
    assert await all_done()
