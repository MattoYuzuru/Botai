"""Claims, input revisions and healthy continuations use independent durable generations."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import contextlib
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update

from botai.contracts.agent_ui import TextBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.execution import guard_job
from botai.core.ids import new_id
from botai.core.jobs import JobClaimLost, JobContext, JobDeferred, JobInputChanged, JobKind
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db.models import Connection, Job, JobAnswer
from botai.jobs.context import PgJobContext
from botai.jobs.notify import Notifier
from botai.jobs.runner import Claimed, Worker
from tests.jobs.env import Env
from tests.jobs.test_conversations import start

pytestmark = pytest.mark.db


def context(env: Env, worker: Worker, claim: Claimed) -> PgJobContext:
    return PgJobContext(
        job_id=claim.id,
        user_id=claim.user_id,
        connection_id=claim.connection_id,
        db=env.sm,
        vault=env.services.vault,
        clock=env.clock,
        notifier=Notifier(None),
        claim_worker=worker.name,
        claim_attempt=claim.attempts,
    )


async def queued(env: Env) -> tuple[str, str, str]:
    user = (await env.account()).id
    _, connection = await env.connection(user)
    job = await env.services.queue.enqueue(JobKind.SYNC, user_id=user, connection_id=connection)
    return user, connection, job


async def test_old_claim_cannot_finish_requeue_publish_or_delete_new_answers(env: Env) -> None:
    user, connection, job_id = await queued(env)
    worker = env.worker({}, name="reused-worker")
    old = await worker.claim()
    assert old is not None
    previous = context(env, worker, old)
    env.advance(minutes=3)
    assert await env.worker({}).recover_orphans() == 1
    with pytest.raises(JobClaimLost):
        await previous.status("Не должно оживить queued")
    current = await worker.claim()
    assert current is not None
    assert current.attempts == old.attempts + 1

    block = new_id()
    await env.services.vault.put(user, block, SecretKind.TRANSIENT, "new-claim-secret")
    async with env.sm() as session:
        session.add(JobAnswer(job_id=job_id, block_id=block, is_secret=True, values=None))
        await session.commit()
    count = len(await env.events(job_id))
    await worker._finish(old, "done", result={"stale": True})
    await worker._fail(old, "internal_error", "Старый отказ", 500)
    await worker._requeue(old, "Старая попытка")
    with pytest.raises(JobClaimLost):
        await previous.emit(TextBlock(id=new_id(), at=env.clock.now(), markdown="Старый ответ"))
    with pytest.raises(JobClaimLost):
        await previous.put_secret(user, connection, SecretKind.SESSION, "stale-session")
    with pytest.raises(JobClaimLost):
        await previous._expire()
    row = await env.job(job_id)
    assert (row.state, row.attempts, row.locked_by) == (
        "running",
        current.attempts,
        worker.name,
    )
    assert row.result is None
    assert row.error is None
    assert len(await env.events(job_id)) == count
    assert await env.services.vault.has(user, block, SecretKind.TRANSIENT)
    assert not await env.services.vault.has(user, connection, SecretKind.SESSION)


async def test_heartbeat_does_not_renew_a_reclaimed_job_with_the_same_worker_name(env: Env) -> None:
    _, _, job_id = await queued(env)
    old_worker = env.worker({}, name="same-name")
    assert await old_worker.claim() is not None
    task = asyncio.create_task(asyncio.sleep(60), name=f"job-{job_id}")
    old_worker._tasks[job_id] = task
    try:
        env.advance(minutes=3)
        assert await env.worker({}).recover_orphans() == 1
        new_worker = env.worker({}, name="same-name")
        assert await new_worker.claim() is not None
        claimed_at = env.clock.now()
        env.advance(seconds=30)
        await old_worker.heartbeat()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        row = await env.job(job_id)
        assert row.locked_at == claimed_at
        assert row.state == "running"
        assert task.cancelled()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_healthy_slices_do_not_consume_transport_retry_attempts(env: Env) -> None:
    calls = 0

    async def handler(
        ctx: JobContext, services: Services, payload: dict[str, Any]
    ) -> dict[str, Any]:
        nonlocal calls
        assert "_retry_failures" not in payload
        calls += 1
        if calls <= 5:
            raise JobDeferred(delay_seconds=1)
        if calls == 6:
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Временный отказ фикстуры")
        return {"ok": True}

    _, _, job_id = await queued(env)
    worker = env.worker({JobKind.SYNC: handler})
    env.start(worker)
    for attempt in range(1, 7):
        row = await env.wait_for(job_id, {"queued"})
        async with asyncio.timeout(5):
            while row.attempts != attempt or row.state != "queued":
                await asyncio.sleep(0.01)
                row = await env.job(job_id)
        assert row.payload["_retry_failures"] == (1 if attempt == 6 else 0)
        assert row.run_after == env.clock.now() + timedelta(seconds=60 if attempt == 6 else 1)
        env.advance(seconds=60 if attempt == 6 else 1)
    done = await env.wait_for(job_id, {"done"})
    assert done.attempts == 7
    assert done.payload["_retry_failures"] == 1


async def test_unmarked_and_current_engine_jobs_run_while_newer_engine_is_parked(
    env: Env,
) -> None:
    user = (await env.account()).id
    future = await env.services.queue.enqueue(
        JobKind.SYNC, user_id=user, payload={"_research_engine": 2}, dedupe=False
    )
    current = await env.services.queue.enqueue(
        JobKind.SYNC, user_id=user, payload={"_research_engine": 1}, dedupe=False
    )
    legacy = await env.services.queue.enqueue(JobKind.SYNC, user_id=user, dedupe=False)
    worker = env.worker({})
    first = await worker.claim()
    assert first is not None
    assert first.id in {legacy, current}
    assert await worker.claim() is None  # one source browser per student
    await worker._finish(first, "done")
    second = await worker.claim()
    assert second is not None
    assert {first.id, second.id} == {legacy, current}
    await worker._finish(second, "done")
    assert await worker.claim() is None
    assert (await env.job(future)).state == "queued"


async def test_claims_serialize_research_per_user_without_blocking_another_user(env: Env) -> None:
    one, two = (await env.account()).id, (await env.account()).id
    first = await env.services.queue.enqueue(JobKind.CONNECT, user_id=one, dedupe=False)
    waiting = await env.services.queue.enqueue(JobKind.CONNECT, user_id=one, dedupe=False)
    await env.services.queue.enqueue(JobKind.CONNECT, user_id=two, dedupe=False)
    workers = [env.worker({}, name=f"research-{i}") for i in range(2)]
    claims = await asyncio.gather(*(worker.claim() for worker in workers))
    assert {claim.user_id for claim in claims if claim is not None} == {one, two}
    assert await env.worker({}).claim() is None
    owner = next(
        (worker, claim)
        for worker, claim in zip(workers, claims, strict=True)
        if claim is not None and claim.user_id == one
    )
    await owner[0]._finish(owner[1], "done")
    next_claim = await env.worker({}).claim()
    assert next_claim is not None
    assert next_claim.id == (waiting if owner[1].id == first else first)


@pytest.mark.parametrize(
    ("active_kind", "waiting_kind"),
    [(JobKind.CONNECT, JobKind.SYNC), (JobKind.SYNC, JobKind.REPAIR)],
)
async def test_sync_and_research_share_one_browser_slot_but_chat_does_not(
    env: Env, active_kind: JobKind, waiting_kind: JobKind
) -> None:
    one, two = (await env.account()).id, (await env.account()).id
    active_id = await env.services.queue.enqueue(active_kind, user_id=one, dedupe=False)
    owner = env.worker({})
    active = await owner.claim()
    assert active is not None
    assert active.id == active_id
    waiting = await env.services.queue.enqueue(waiting_kind, user_id=one, dedupe=False)
    other = await env.services.queue.enqueue(waiting_kind, user_id=two, dedupe=False)
    chat = await env.services.queue.enqueue(JobKind.CHAT, user_id=one, dedupe=False)
    second = await env.worker({}).claim()
    assert second is not None
    independent = await env.worker({}).claim()
    assert independent is not None
    assert {second.id, independent.id} == {other, chat}
    assert (await env.job(waiting)).state == "queued"
    await owner._finish(active, "done")
    continued = await env.worker({}).claim()
    assert continued is not None
    assert continued.id == waiting


async def test_new_inbox_input_invalidates_only_until_that_inbox_is_read(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    worker = env.worker({})
    claim = await worker.claim()
    assert claim is not None
    assert claim.id == job_id
    ctx = context(env, worker, claim)
    response = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={"text": "Только расписание", "targetJobId": job_id, "clientMessageId": new_id()},
    )
    assert response.status_code == 202
    await ctx.status("Проверяю источник")
    assert ctx.input_revision == 0
    async with env.sm() as session:
        with pytest.raises(JobInputChanged):
            await guard_job(ctx, session, check_input=True)
    with pytest.raises(JobInputChanged):
        await worker._finish(claim, "done", result={"old": True}, input_revision=0)
    assert (await env.job(job_id)).state == "running"
    assert ctx.input_revision == 0
    turns = await ctx.drain_dialogue()
    assert [turn.text for turn in turns] == ["Только расписание"]
    assert ctx.input_revision == 1
    async with env.sm() as session:
        await guard_job(ctx, session, check_input=True)
    assert user == claim.user_id


async def test_a_changed_job_binding_revokes_its_old_source_context(env: Env) -> None:
    _, _, job_id = await queued(env)
    worker = env.worker({})
    claim = await worker.claim()
    assert claim is not None
    old = context(env, worker, claim)
    async with env.sm() as db, db.begin():
        await db.execute(update(Job).where(Job.id == job_id).values(connection_id=None))
    with pytest.raises(JobClaimLost):
        await old.status("Поздний статус другого подключения")
    assert (await env.job(job_id)).attempts == claim.attempts


@pytest.mark.parametrize(
    ("configured", "other_active", "expected"),
    [(False, False, "draft"), (True, False, "stale"), (True, True, "learning")],
)
async def test_cancel_restores_only_the_stopped_sources_transient_status(
    env: Env, configured: bool, other_active: bool, expected: str
) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    _, connection = await env.connection(user, status="learning")
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=user, connection_id=connection
    )
    worker = env.worker({})
    assert await worker.claim() is not None
    next_sync = env.clock.now() + timedelta(hours=3)
    async with env.sm() as session:
        await session.execute(
            update(Connection)
            .where(Connection.id == connection)
            .values(adapter="recipe" if configured else None, next_sync_at=next_sync)
        )
        if other_active:
            session.add(
                Job(
                    id=new_id(),
                    user_id=user,
                    connection_id=connection,
                    kind="sync",
                    state="running",
                    attempts=1,
                    locked_by="other-source-owner",
                    locked_at=env.clock.now(),
                    run_after=env.clock.now(),
                    payload={},
                )
            )
        await session.commit()
    response = await client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert response.status_code == 204
    assert (await env.job(job_id)).state == "cancelled"
    async with env.sm() as session:
        row = await session.get(Connection, connection)
        assert row is not None
        assert row.status == expected
        assert row.next_sync_at == next_sync
