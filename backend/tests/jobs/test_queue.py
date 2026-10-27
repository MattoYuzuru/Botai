"""PgJobQueue: deduplication of in-flight jobs (NFR-006)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from botai.core.jobs import JobKind
from botai.db.models import Job
from tests.jobs.env import Env

pytestmark = pytest.mark.db


async def test_enqueue_creates_a_queued_job_with_its_first_event(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT,
        user_id=acc.id,
        connection_id=conn,
        payload={"url": "https://x.example.org"},
    )
    job = await env.job(job_id)
    assert (job.kind, job.state, job.user_id, job.connection_id) == (
        "connect",
        "queued",
        acc.id,
        conn,
    )
    assert job.payload == {"url": "https://x.example.org"}
    assert job.attempts == 0
    assert job.run_after == env.clock.now()
    events = await env.events(job_id)
    assert [(e.seq, e.type) for e in events] == [(1, "status")]
    assert events[0].data == {"state": "queued", "stage": None, "startedAt": None, "elapsedMs": 0}


async def test_a_job_in_flight_is_returned_instead_of_a_second_one(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    q = env.services.queue
    first = await q.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn)
    assert await q.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn) == first
    for state in ("running", "waiting_user"):
        async with env.sm() as s:
            await s.execute(update(Job).where(Job.id == first).values(state=state))
            await s.commit()
        assert await q.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn) == first, state


async def test_a_finished_job_does_not_block_a_new_one(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    q = env.services.queue
    first = await q.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn)
    for state in ("done", "failed", "cancelled"):
        async with env.sm() as s:
            await s.execute(update(Job).where(Job.id == first).values(state=state))
            await s.commit()
        again = await q.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn)
        assert again != first, state
        first = again


async def test_dedupe_is_per_kind_and_per_connection(env: Env) -> None:
    acc = await env.account()
    _, c1 = await env.connection(acc.id)
    _, c2 = await env.connection(acc.id)
    q = env.services.queue
    ids = {
        await q.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=c1),
        await q.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=c2),
        await q.enqueue(JobKind.REPAIR, user_id=acc.id, connection_id=c1),
    }
    assert len(ids) == 3


async def test_no_dedupe_when_not_asked_or_without_a_connection(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    q = env.services.queue
    a = await q.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn, dedupe=False)
    b = await q.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn, dedupe=False)
    c = await q.enqueue(JobKind.CHAT, user_id=acc.id, thread_id="t1")
    d = await q.enqueue(JobKind.CHAT, user_id=acc.id, thread_id="t1")
    assert len({a, b, c, d}) == 4


async def test_racing_enqueues_make_one_job(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    ids = await asyncio.gather(
        *(
            env.services.queue.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn)
            for _ in range(12)
        )
    )
    assert len(set(ids)) == 1
    async with env.sm() as s:
        jobs = (await s.scalars(select(Job).where(Job.connection_id == conn))).all()
    assert len(jobs) == 1


async def test_a_delayed_retry_is_pulled_forward_by_a_manual_request(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    q = env.services.queue
    job_id = await q.enqueue(
        JobKind.SYNC,
        user_id=acc.id,
        connection_id=conn,
        run_after=env.clock.now() + timedelta(minutes=30),
    )
    assert (await env.job(job_id)).run_after == env.clock.now() + timedelta(minutes=30)
    assert await q.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn) == job_id
    assert (
        await env.job(job_id)
    ).run_after == env.clock.now()  # "sync now" does not wait for backoff


async def test_run_after_is_kept_for_a_new_delayed_job(env: Env) -> None:
    acc = await env.account()
    later = env.clock.now() + timedelta(hours=1)
    job_id = await env.services.queue.enqueue(JobKind.ACTION, user_id=acc.id, run_after=later)
    assert (await env.job(job_id)).run_after == later
