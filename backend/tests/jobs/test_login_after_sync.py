"""A sync that ended because the student must log in again opens that login in the source's
thread, once (phase 2 of the unified agent)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select, update

from botai.core.handlers import HandlerTable
from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services
from botai.db.models import ChatThread, Connection, Job
from botai.jobs.runner import Claimed
from tests.jobs.env import Env

pytestmark = pytest.mark.db

Payload = dict[str, Any]


async def hold_login(ctx: JobContext, services: Services, payload: Payload) -> Payload:
    await asyncio.Event().wait()  # the login waits for the student for as long as the test looks
    return {}


def handlers(result: Payload) -> HandlerTable:
    async def sync(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        return result

    return {JobKind.SYNC: sync, JobKind.CONNECT: hold_login}


async def source(
    env: Env, *, status: str = "needs_reauth", adapter: str | None = "recipe"
) -> tuple[str, str]:
    acc = await env.account()
    _, conn = await env.connection(acc.id, status=status)
    async with env.sm() as session:
        await session.execute(update(Connection).where(Connection.id == conn).values(adapter=adapter))
        await session.commit()
    return acc.id, conn


async def sync_once(env: Env, user: str, conn: str) -> str:
    job_id = await env.services.queue.enqueue(
        JobKind.SYNC, user_id=user, connection_id=conn, dedupe=False
    )
    await env.wait_for(job_id, {"done", "failed"})
    return job_id


async def logins(env: Env, user: str, *, count: int | None = None) -> list[Job]:
    """The login tasks of the student. The runner opens one right after the sync is marked done,
    so a test that expects `count` of them waits for that moment; none is checked after a pause."""
    for _ in range(150):
        async with env.sm() as session:
            found = list(
                await session.scalars(
                    select(Job).where(Job.user_id == user, Job.kind == JobKind.CONNECT.value)
                )
            )
        if count is None or len(found) >= count:
            return found
        await asyncio.sleep(0.02)
    return found


async def test_a_sync_that_needs_a_login_opens_it_in_the_thread_of_the_source(env: Env) -> None:
    user, conn = await source(env)
    env.start(env.worker(handlers({"status": "needs_reauth"}), shutdown_grace=0.2))
    sync = await sync_once(env, user, conn)
    (login,) = await logins(env, user, count=1)
    assert login.connection_id == conn
    assert login.payload["manual_login"] is True
    assert login.thread_id is not None
    async with env.sm() as session:
        thread = await session.get(ChatThread, login.thread_id)
        assert thread is not None
        assert thread.user_id == user
        sync_row = await session.get(Job, sync)
    assert sync_row is not None
    assert sync_row.state == "done"  # the sync itself ended as it did; the login is a new task


async def test_a_second_ending_of_a_sync_does_not_open_a_second_login(env: Env) -> None:
    """While the login waits for the student the source is busy, so no sync runs; but a sync that
    ended at the same moment from another worker, or a retried hook, finds the open login."""
    user, conn = await source(env)
    worker = env.worker(handlers({"status": "needs_reauth"}), shutdown_grace=0.2)
    ended = Claimed(id="x", user_id=user, kind="sync", connection_id=conn, payload={}, attempts=1)
    await worker._ask_for_login_after(ended, {"status": "needs_reauth"})
    (first,) = await logins(env, user, count=1)
    await worker._ask_for_login_after(ended, {"status": "needs_reauth"})
    await asyncio.sleep(0.2)
    (second,) = await logins(env, user)
    assert second.id == first.id
    assert second.thread_id == first.thread_id


async def test_the_login_goes_to_the_thread_the_source_already_has(env: Env) -> None:
    user, conn = await source(env)
    env.start(env.worker(handlers({"status": "needs_reauth"}), shutdown_grace=0.2))
    await sync_once(env, user, conn)
    (login,) = await logins(env, user, count=1)
    async with env.sm() as session:
        await session.execute(update(Job).where(Job.id == login.id).values(state="cancelled"))
        await session.commit()
    await sync_once(env, user, conn)
    again = await logins(env, user, count=2)
    assert len(again) == 2
    assert {j.thread_id for j in again} == {login.thread_id}  # the same chat, not a new one


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
async def test_no_login_is_opened_when_it_is_not_the_students_to_do(
    env: Env, result: Payload, status: str, adapter: str | None
) -> None:
    user, conn = await source(env, status=status, adapter=adapter)
    env.start(env.worker(handlers(result), shutdown_grace=0.2))
    await sync_once(env, user, conn)
    await asyncio.sleep(0.3)
    assert await logins(env, user) == []
