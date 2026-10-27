"""The worker: claiming, retries, budgets, recovery, scheduling (lld.md §4-5, NFR-006, NFR-012)."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from botai.agent.llm import current_scope
from botai.core.errors import BotaiError, ErrorCode
from botai.core.handlers import HandlerTable
from botai.core.jobs import JobCancelled, JobContext, JobKind, JobState
from botai.core.services import Services
from botai.db.models import Connection, Job, User
from botai.jobs.runner import RunnerConfig, run_forever, schedule_syncs
from tests.jobs.env import Env

pytestmark = pytest.mark.db

Payload = dict[str, Any]


async def _queue(env: Env, kind: JobKind = JobKind.SYNC, **kw: Any) -> tuple[str, str, str]:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(kind, user_id=acc.id, connection_id=conn, **kw)
    return acc.id, conn, job_id


async def _requeued(env: Env, job_id: str, attempts: int) -> Job:
    """The job after its `attempts`-th failure, waiting in the queue for its backoff."""
    for _ in range(500):
        job = await env.job(job_id)
        if job.state == "queued" and job.attempts == attempts:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} never came back to the queue after attempt {attempts}")


async def test_a_job_runs_and_its_story_is_recorded(env: Env) -> None:
    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        await ctx.status("Читаю расписание")
        return {"rows": 3, "payload": payload}

    _, _, job_id = await _queue(env, payload={"hint": 1})
    env.start(env.worker({JobKind.SYNC: handler}))
    job = await env.wait_for(job_id, {"done"})

    assert job.result == {"rows": 3, "payload": {"hint": 1}}
    assert job.attempts == 1
    assert job.locked_by is None
    assert job.started_at is not None
    assert job.finished_at is not None
    events = await env.events(job_id)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [(e.type, e.data.get("state"), e.data.get("stage")) for e in events] == [
        ("status", "queued", None),
        ("status", "running", None),
        ("status", "running", "Читаю расписание"),
        ("status", "done", "Читаю расписание"),
        ("done", None, None),
    ]
    assert events[-1].data == {"result": {"rows": 3, "payload": {"hint": 1}}}


async def test_two_workers_never_take_the_same_job(env: Env) -> None:
    acc = await env.account()
    ran: dict[str, list[str]] = {"a": [], "b": []}

    def table(who: str) -> HandlerTable:
        async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
            await asyncio.sleep(0.02)  # long enough for the other worker to look at the queue
            ran[who].append(ctx.job_id)

        return {JobKind.CHAT: handler}

    ids = [
        await env.services.queue.enqueue(JobKind.CHAT, user_id=acc.id, thread_id=f"t{n}")
        for n in range(40)
    ]
    env.start(env.worker(table("a"), concurrency=3, name="worker-a"))
    env.start(env.worker(table("b"), concurrency=3, name="worker-b"))
    for job_id in ids:
        await env.wait_for(job_id, {"done"})

    everything = Counter(ran["a"] + ran["b"])
    assert sorted(everything) == sorted(ids)
    assert set(everything.values()) == {1}, "a job ran twice"
    assert ran["a"], "worker a got nothing"
    assert ran["b"], "worker b got nothing"
    for job_id in ids:
        assert (await env.job(job_id)).attempts == 1


async def test_concurrency_is_bounded(env: Env) -> None:
    acc = await env.account()
    active = 0
    peak = 0

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1

    ids = [
        await env.services.queue.enqueue(JobKind.CHAT, user_id=acc.id, thread_id=f"t{n}")
        for n in range(8)
    ]
    env.start(env.worker({JobKind.CHAT: handler}, concurrency=2))
    for job_id in ids:
        await env.wait_for(job_id, {"done"})
    assert peak == 2


# --- failures and retries -------------------------------------------------------------------------


async def test_an_unavailable_source_is_retried_after_1_then_5_minutes(env: Env) -> None:
    calls = 0

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Сайт вуза не отвечает")
        return {"calls": calls}

    _, _, job_id = await _queue(env)
    env.start(env.worker({JobKind.SYNC: handler}))
    start = env.clock.now()

    job = await _requeued(env, job_id, 1)
    assert job.run_after == start + timedelta(minutes=1)
    assert job.stage is not None
    assert "1 мин" in job.stage
    await asyncio.sleep(0.3)
    assert calls == 1, "the backoff must hold"

    env.advance(seconds=61)
    job = await _requeued(env, job_id, 2)
    assert job.run_after == env.clock.now() + timedelta(minutes=5)

    env.advance(minutes=5)
    job = await env.wait_for(job_id, {"done"})
    assert (job.attempts, job.result, calls) == (3, {"calls": 3}, 3)
    assert job.error is None


async def test_retries_stop_after_the_third_backoff(env: Env) -> None:
    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        raise BotaiError(ErrorCode.SOURCE_RATE_LIMITED, "Слишком часто")

    _, conn, job_id = await _queue(env)
    env.start(env.worker({JobKind.SYNC: handler}))
    delays = []
    for attempt in (1, 2, 3):
        job = await _requeued(env, job_id, attempt)
        delays.append(job.run_after - env.clock.now())
        env.advance(minutes=31)
    job = await env.wait_for(job_id, {"failed"})
    assert delays == [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30)]
    assert job.attempts == 4
    assert job.error is not None
    assert job.error["code"] == "source_rate_limited"
    events = await env.events(job_id)
    assert events[-1].type == "error"
    assert events[-1].data["code"] == "source_rate_limited"
    assert events[-1].data["jobId"] == job_id
    assert events[-1].data["connectionId"] == conn
    assert events[-1].data["status"] == 400


async def test_other_errors_fail_at_once_with_a_problem_event(env: Env) -> None:
    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        raise BotaiError(ErrorCode.LOGIN_FAILED, "Вуз не принял пароль")

    _, _, job_id = await _queue(env, JobKind.CONNECT)
    env.start(env.worker({JobKind.CONNECT: handler}))
    job = await env.wait_for(job_id, {"failed"})
    assert job.attempts == 1
    assert job.error == {"code": "login_failed", "detail": "Вуз не принял пароль", "status": 400}
    assert job.finished_at is not None
    events = await env.events(job_id)
    assert [e.type for e in events[-2:]] == ["status", "error"]
    assert events[-2].data["state"] == "failed"
    assert events[-1].data["detail"] == "Вуз не принял пароль"
    assert events[-1].data["type"].endswith("/login_failed")


async def test_a_crash_is_reported_without_its_message(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    leak = "tok-9f3a-SECRET https://lms.example.org/api?token=abc123"

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        raise RuntimeError(leak)

    _, _, job_id = await _queue(env)
    with caplog.at_level(logging.DEBUG):
        env.start(env.worker({JobKind.SYNC: handler}))
        job = await env.wait_for(job_id, {"failed"})
    assert job.error is not None
    assert job.error["code"] == "internal_error"
    for text in (str(job.error), *(str(e.data) for e in await env.events(job_id)), caplog.text):
        assert "tok-9f3a" not in text
        assert "abc123" not in text
    assert "RuntimeError" in caplog.text  # the type and frames are logged, the message is not


async def test_job_cancelled_by_the_handler_ends_cancelled(env: Env) -> None:
    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        raise JobCancelled("the user left")

    _, _, job_id = await _queue(env, JobKind.CONNECT)
    env.start(env.worker({JobKind.CONNECT: handler}))
    job = await env.wait_for(job_id, {"cancelled"})
    assert job.error is None
    assert job.finished_at is not None
    assert (await env.events(job_id))[-1].data["state"] == "cancelled"


async def test_a_kind_without_a_handler_fails_clearly(env: Env) -> None:
    _, _, job_id = await _queue(env, JobKind.INGEST)
    env.start(env.worker({}))
    job = await env.wait_for(job_id, {"failed"})
    assert job.error is not None
    assert job.error["code"] == "not_implemented"
    assert job.error["status"] == 501


# --- budgets --------------------------------------------------------------------------------------


async def test_a_job_over_its_budget_is_treated_as_an_unavailable_source(env: Env) -> None:
    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        await asyncio.sleep(30)

    _, _, job_id = await _queue(env)
    env.start(env.worker({JobKind.SYNC: handler}, budgets={"sync": 0.2}))
    job = await _requeued(env, job_id, 1)
    assert job.run_after == env.clock.now() + timedelta(minutes=1)


async def test_a_human_step_reported_through_status_does_not_spend_the_budget(env: Env) -> None:
    """The login flow shows a fallback screen and reports `waiting_user` itself, without ask()."""

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        await ctx.status("Жду, пока вы пройдёте шаг на экране", state=JobState.WAITING_USER)
        await asyncio.sleep(0.7)  # more than the 0.3 s budget
        await ctx.status("Вошёл")
        return {"ok": True}

    _, _, job_id = await _queue(env)
    env.start(env.worker({JobKind.SYNC: handler}, budgets={"sync": 0.3}))
    job = await env.wait_for(job_id, {"done"})
    assert job.attempts == 1  # the budget never fired


# --- recovery and heartbeat -----------------------------------------------------------------------


async def test_a_dead_workers_job_goes_back_to_the_queue(env: Env) -> None:
    ran: list[int] = []

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        ran.append(1)
        return {"ok": True}

    _, _, job_id = await _queue(env)
    dead = env.worker({JobKind.SYNC: handler}, name="dead-worker")
    claimed = await dead.claim()  # it took the job and then died without a trace
    assert claimed is not None
    assert (await env.job(job_id)).state == "running"

    survivor = env.worker({JobKind.SYNC: handler}, name="survivor")
    assert await survivor.recover_orphans() == 0  # too early: the lease has not expired
    env.advance(minutes=3)
    assert await survivor.recover_orphans() == 1
    job = await env.job(job_id)
    assert (job.state, job.attempts, job.locked_by) == ("queued", 1, None)

    env.start(survivor)
    done = await env.wait_for(job_id, {"done"})
    assert done.attempts == 2
    assert ran == [1]


async def test_a_job_that_keeps_killing_workers_is_failed(env: Env) -> None:
    _, _, job_id = await _queue(env)
    w = env.worker({})
    async with env.sm() as s:
        await s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(state="running", attempts=4, locked_by="gone", locked_at=env.clock.now())
        )
        await s.commit()
    env.advance(minutes=3)
    assert await w.recover_orphans() == 1
    job = await env.job(job_id)
    assert job.state == "failed"
    assert job.error is not None
    assert job.error["code"] == "internal_error"
    assert (await env.events(job_id))[-1].type == "error"


async def test_heartbeat_keeps_a_live_job_from_being_recovered(env: Env) -> None:
    release = asyncio.Event()

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        await release.wait()

    _, _, job_id = await _queue(env)
    w = env.worker({JobKind.SYNC: handler}, name="alive")
    env.start(w)
    await env.wait_for(job_id, {"running"})
    env.advance(minutes=1, seconds=30)
    await w.heartbeat()
    env.advance(minutes=1)  # 2.5 minutes since the claim, 1 since the heartbeat
    assert (await env.job(job_id)).locked_at == env.clock.now() - timedelta(minutes=1)
    assert await env.worker({}).recover_orphans() == 0
    release.set()
    await env.wait_for(job_id, {"done"})


# --- cancellation, deletion, shutdown ---------------------------------------------------------------


@pytest.mark.parametrize("notify", [True, False])
async def test_a_running_job_is_stopped_when_the_student_cancels(env: Env, notify: bool) -> None:
    stopped = asyncio.Event()

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            stopped.set()
            raise

    client = env.new_client()
    acc = await env.account(client)
    _, conn = await env.connection(acc.id)
    job_id = await env.services.queue.enqueue(JobKind.CHAT, user_id=acc.id, thread_id="t")
    w = env.worker({JobKind.CHAT: handler}, notify=notify)
    env.start(w)
    await env.wait_for(job_id, {"running"})

    assert (await client.post(f"/api/v1/jobs/{job_id}/cancel")).status_code == 204
    if not notify:
        await w.heartbeat()  # without NOTIFY the heartbeat notices it
    await asyncio.wait_for(stopped.wait(), 5)
    job = await env.wait_for(job_id, {"cancelled"})
    assert job.result is None
    assert conn  # the connection is untouched by the cancellation


async def test_a_job_deleted_under_the_handler_ends_the_handler_not_the_worker(env: Env) -> None:
    go = asyncio.Event()
    outcome: list[str] = []

    async def doomed(ctx: JobContext, services: Services, payload: Payload) -> None:
        await go.wait()
        try:
            await ctx.status("ещё работаю")
        except JobCancelled:
            outcome.append("cancelled")
            raise

    async def fine(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        return {"fine": True}

    client = env.new_client()
    acc = await env.account(client)
    _, conn = await env.connection(acc.id)
    doomed_id = await env.services.queue.enqueue(JobKind.SYNC, user_id=acc.id, connection_id=conn)
    env.start(env.worker({JobKind.SYNC: doomed, JobKind.CHAT: fine}))
    await env.wait_for(doomed_id, {"running"})

    assert (
        await client.delete(f"/api/v1/connections/{conn}")
    ).status_code == 204  # cascades the job
    go.set()
    for _ in range(100):
        if outcome:
            break
        await asyncio.sleep(0.02)
    assert outcome == ["cancelled"]

    later = await env.services.queue.enqueue(JobKind.CHAT, user_id=acc.id, thread_id="t")
    assert (await env.wait_for(later, {"done"})).result == {"fine": True}


async def test_shutdown_hands_an_unfinished_job_back(env: Env) -> None:
    started = asyncio.Event()

    async def slow(ctx: JobContext, services: Services, payload: Payload) -> None:
        started.set()
        await asyncio.sleep(60)

    _, _, job_id = await _queue(env)
    w = env.worker({JobKind.SYNC: slow}, shutdown_grace=0.1)
    task = env.start(w)
    await asyncio.wait_for(started.wait(), 5)
    w.stop()
    await asyncio.wait_for(task, 5)
    job = await env.job(job_id)
    assert job.state == "queued"  # another worker (or this one after restart) picks it up
    assert job.locked_by is None
    assert job.attempts == 1


async def test_shutdown_lets_short_jobs_finish(env: Env) -> None:
    started = asyncio.Event()

    async def short(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        started.set()
        await asyncio.sleep(0.2)
        return {"finished": True}

    _, _, job_id = await _queue(env)
    w = env.worker({JobKind.SYNC: short}, shutdown_grace=5)
    task = env.start(w)
    await asyncio.wait_for(started.wait(), 5)
    w.stop()
    await asyncio.wait_for(task, 5)
    assert (await env.job(job_id)).state == "done"


async def test_jobs_of_a_disabled_user_wait(env: Env) -> None:
    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        return {"ran": True}

    user, _, job_id = await _queue(env)
    async with env.sm() as s:
        await s.execute(update(User).where(User.id == user).values(disabled=True))
        await s.commit()
    env.start(env.worker({JobKind.SYNC: handler}))
    await asyncio.sleep(0.4)
    assert (await env.job(job_id)).state == "queued"
    async with env.sm() as s:
        await s.execute(update(User).where(User.id == user).values(disabled=False))
        await s.commit()
    await env.wait_for(job_id, {"done"})


# --- the sync scheduler ---------------------------------------------------------------------------


async def test_the_scheduler_enqueues_sync_for_due_live_connections_only(env: Env) -> None:
    acc = await env.account()
    due = env.clock.now() - timedelta(minutes=1)
    later = env.clock.now() + timedelta(minutes=30)
    wanted: dict[str, str] = {}
    for name, status, when in (
        ("active-due", "active", due),
        ("stale-due", "stale", due),
        ("active-later", "active", later),
        ("active-never", "active", None),
        ("draft", "draft", due),
        ("learning", "learning", due),
        ("disabled", "disabled", due),
        ("error", "error", due),
        ("repairing", "repairing", due),
        ("needs_reauth", "needs_reauth", due),
    ):
        _, conn = await env.connection(acc.id, status=status)
        async with env.sm() as s:
            await s.execute(
                update(Connection).where(Connection.id == conn).values(next_sync_at=when)
            )
            await s.commit()
        wanted[name] = conn

    assert await schedule_syncs(env.services) == 2
    async with env.sm() as s:
        queued = set(
            await s.scalars(
                select(Job.connection_id).where(Job.kind == "sync", Job.user_id == acc.id)
            )
        )
    assert queued == {wanted["active-due"], wanted["stale-due"]}


async def test_the_scheduler_does_not_pile_up_jobs(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    async with env.sm() as s:
        await s.execute(
            update(Connection).where(Connection.id == conn).values(next_sync_at=env.clock.now())
        )
        await s.commit()
    first = await schedule_syncs(env.services)
    second = await schedule_syncs(env.services)
    assert (first, second) == (1, 1)  # it looked twice ...
    async with env.sm() as s:
        count = len((await s.scalars(select(Job.id).where(Job.connection_id == conn))).all())
    assert count == 1  # ... and the queue made one job


async def test_the_scheduler_skips_disabled_users(env: Env) -> None:
    acc = await env.account()
    _, conn = await env.connection(acc.id)
    async with env.sm() as s:
        await s.execute(
            update(Connection).where(Connection.id == conn).values(next_sync_at=env.clock.now())
        )
        await s.execute(update(User).where(User.id == acc.id).values(disabled=True))
        await s.commit()
    assert await schedule_syncs(env.services) == 0


async def test_run_forever_is_the_worker_entry_point(env: Env) -> None:
    async def handler(ctx: JobContext, services: Services, payload: Payload) -> Payload:
        return {"via": "run_forever"}

    _, _, job_id = await _queue(env)
    stop = asyncio.Event()
    cfg = RunnerConfig(
        poll_seconds=0.05, heartbeat_seconds=3600, maintenance_seconds=3600, scheduler_seconds=3600
    )
    task = asyncio.create_task(
        run_forever({JobKind.SYNC: handler}, services=env.services, config=cfg, stop=stop)
    )
    try:
        job = await env.wait_for(job_id, {"done"})
        assert job.result == {"via": "run_forever"}
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_handlers_run_inside_the_users_llm_usage_scope(env: Env) -> None:
    """Quota and `llm_usage` need to know whose LLM call it is, whichever module makes it."""
    seen: list[tuple[str, str | None] | None] = []

    async def handler(ctx: JobContext, services: Services, payload: Payload) -> None:
        scope = current_scope()
        seen.append((scope.user_id, scope.job_id) if scope else None)

    user_id, _, job_id = await _queue(env)
    env.start(env.worker({JobKind.SYNC: handler}))
    await env.wait_for(job_id, {"done"})
    assert seen == [(user_id, job_id)]
    assert current_scope() is None  # nothing leaks out of the job
