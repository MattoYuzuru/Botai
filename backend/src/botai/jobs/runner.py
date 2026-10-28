"""Owner: W1. The worker loop: claim jobs with SELECT ... FOR UPDATE SKIP LOCKED, run handlers with
a PgJobContext, retry with backoff (lld.md §5), recover orphans, tick the sync scheduler.

Job life cycle (lld.md §4): queued -> running <-> waiting_user -> done | failed | cancelled, and
running -> queued for a retry. A claim bumps `attempts`; a retryable failure (the source was
unavailable or rate-limited) goes back to `queued` after 1, 5 and 30 minutes, then fails. A worker
that dies leaves its jobs with a stale `locked_at`; the maintenance pass puts them back in the queue
(handlers must be idempotent, NFR-012).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.llm import usage_scope
from botai.core.diagnostics import diagnostic_event, diagnostic_scope, purge_diagnostics
from botai.core.errors import BotaiError, ErrorCode
from botai.core.handlers import Handler, HandlerTable
from botai.core.ids import new_id
from botai.core.jobs import JobCancelled, JobKind
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db.models import Connection, Job, JobAnswer, User
from botai.jobs import dialogue
from botai.jobs.context import ASK_TTL, PgJobContext
from botai.jobs.events import (
    TERMINAL,
    append_event,
    error_event,
    purge_finished,
    status_data,
)
from botai.jobs.notify import CHANNEL_CANCEL, CHANNEL_JOBS, Notifier, dsn_for_asyncpg

log = logging.getLogger(__name__)

RETRYABLE = frozenset({ErrorCode.SOURCE_UNAVAILABLE, ErrorCode.SOURCE_RATE_LIMITED})
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
SYNC_STATUSES = ("active", "stale")

# Active-time budgets per kind (lld.md §5). Time spent waiting for the student is not counted.
BUDGETS: dict[str, float] = {
    JobKind.SYNC.value: 120.0,
    JobKind.ACTION.value: 120.0,
    JobKind.CHAT.value: 300.0,
    JobKind.INGEST.value: 600.0,
    JobKind.CONNECT.value: 1800.0,
    JobKind.LEARN.value: 1800.0,
    JobKind.REPAIR.value: 1800.0,
}


@dataclass(slots=True)
class RunnerConfig:
    poll_seconds: float = 2.0
    concurrency: int = 4
    heartbeat_seconds: float = 15.0
    lease: timedelta = timedelta(minutes=2)
    maintenance_seconds: float = 30.0
    scheduler_seconds: float = 60.0
    scheduler_batch: int = 100
    shutdown_grace: float = 20.0
    ask_ttl: timedelta = ASK_TTL
    budgets: dict[str, float] = field(default_factory=lambda: dict(BUDGETS))


@dataclass(frozen=True, slots=True)
class Claimed:
    id: str
    user_id: str
    kind: str
    connection_id: str | None
    payload: dict[str, Any]
    attempts: int
    thread_id: str | None = None


class _Budget:
    """The active-time budget of a job. Waiting for the student stops the clock."""

    def __init__(self, scope: asyncio.Timeout) -> None:
        self._scope = scope
        self._left = 0.0
        self._paused = 0

    def pause(self) -> None:
        if self._paused == 0:
            when = self._scope.when()
            self._left = max(0.0, when - asyncio.get_running_loop().time()) if when else 0.0
            self._reschedule(None)
        self._paused += 1

    def resume(self) -> None:
        self._paused = max(0, self._paused - 1)
        if self._paused == 0:
            self._reschedule(asyncio.get_running_loop().time() + self._left)

    def _reschedule(self, when: float | None) -> None:
        with contextlib.suppress(RuntimeError):  # already expired or left: nothing to move
            self._scope.reschedule(when)


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{new_id()[-6:]}"[:64]


class Worker:
    def __init__(
        self,
        handlers: HandlerTable,
        services: Services,
        *,
        config: RunnerConfig | None = None,
        notifier: Notifier | None = None,
        name: str | None = None,
    ) -> None:
        self.handlers = handlers
        self.services = services
        self.config = config or RunnerConfig()
        self.notifier = notifier or Notifier(dsn_for_asyncpg(services.settings.database_url))
        self.name = name or worker_name()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_reason: dict[str, str] = {}
        self._stop = asyncio.Event()
        self._slot_free = asyncio.Event()

    # --- claiming ---------------------------------------------------------------------------

    async def claim(self) -> Claimed | None:
        now = self.services.clock.now()
        async with self.services.db() as session:
            job = await session.scalar(
                select(Job)
                .join(User, User.id == Job.user_id)
                .where(Job.state == "queued", Job.run_after <= now, User.disabled.is_(False))
                .order_by(Job.run_after, Job.id)
                .limit(1)
                .with_for_update(of=Job, skip_locked=True)
            )
            if job is None:
                return None
            job.state = "running"
            job.attempts += 1
            job.locked_by = self.name
            job.locked_at = now
            job.started_at = job.started_at or now
            job.error = None
            await append_event(session, job.id, "status", status_data(job, now))
            await session.commit()
            return Claimed(
                job.id,
                job.user_id,
                job.kind,
                job.connection_id,
                dict(job.payload),
                job.attempts,
                job.thread_id,
            )

    # --- running one job --------------------------------------------------------------------

    async def run_job(self, job: Claimed) -> None:
        started = time.monotonic()
        with diagnostic_scope(
            job_id=job.id,
            thread_id=job.thread_id,
            connection_id=job.connection_id,
            job_kind=job.kind,
        ):
            diagnostic_event("job.start", attempt=job.attempts)
            try:
                await self._run_job(job)
            finally:
                diagnostic_event("job.exit", elapsed_ms=int((time.monotonic() - started) * 1000))

    async def _run_job(self, job: Claimed) -> None:
        handler: Handler | None = None
        with contextlib.suppress(ValueError):
            handler = self.handlers.get(JobKind(job.kind))
        if handler is None:
            await self._fail(
                job, ErrorCode.NOT_IMPLEMENTED.value, "Обработчик задания не подключён", 501
            )
            return

        budget = self.config.budgets.get(job.kind, 300.0)
        scope = asyncio.timeout(budget)
        clock = _Budget(scope)

        ctx = PgJobContext(
            job_id=job.id,
            user_id=job.user_id,
            connection_id=job.connection_id,
            db=self.services.db,
            vault=self.services.vault,
            clock=self.services.clock,
            notifier=self.notifier,
            ask_ttl=self.config.ask_ttl,
            poll_seconds=self.config.poll_seconds,
            on_wait_start=clock.pause,
            on_wait_end=clock.resume,
        )
        try:
            async with scope:
                # every LLM call of the handler is quota-checked and recorded for this user and job
                with usage_scope(job.user_id, job.id):
                    result = await handler(ctx, self.services, job.payload)
        except JobCancelled as exc:
            log.info("job %s cancelled: %s", job.id, exc)
            await self._finish(job, "cancelled")
        except asyncio.CancelledError:
            reason = self._cancel_reason.pop(job.id, "shutdown")
            if reason == "shutdown":
                await self._requeue(job, "Продолжу после перезапуска")
            else:
                await self._finish(job, "cancelled")
            if reason == "shutdown":
                raise
        except TimeoutError:
            if not scope.expired():
                await self._fail_unexpected(job, "TimeoutError")
                return
            await self._failed_or_retry(
                job, BotaiError(ErrorCode.SOURCE_UNAVAILABLE, "Задание не уложилось во время")
            )
        except BotaiError as err:
            await self._failed_or_retry(job, err)
        except Exception as exc:
            await self._fail_unexpected(job, type(exc).__name__, exc.__traceback__)
        else:
            await self._finish(job, "done", result=result)

    async def _failed_or_retry(self, job: Claimed, err: BotaiError) -> None:
        if err.code in RETRYABLE and job.attempts <= len(RETRY_DELAYS):
            delay = RETRY_DELAYS[job.attempts - 1]
            minutes = int(delay.total_seconds() // 60)
            await self._requeue(
                job, f"Источник недоступен, повторю через {minutes} мин", delay=delay
            )
            return
        await self._fail(
            job, err.code.value, err.detail or "Задание не выполнено", err.status, extra=err.extra
        )

    async def _fail_unexpected(self, job: Claimed, kind: str, tb: Any = None) -> None:
        # Type and frames only: an exception message may carry a URL with a token or page text.
        frames = "".join(traceback.format_tb(tb)) if tb is not None else ""
        log.error("job %s (%s) crashed with %s\n%s", job.id, job.kind, kind, frames)
        await self._fail(job, "internal_error", "Внутренняя ошибка, мы уже знаем", 500)

    # --- state transitions (each in one transaction with its events) --------------------------

    async def _locked(self, session: AsyncSession, job_id: str) -> Job | None:
        job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.state in TERMINAL:
            return None  # deleted with its connection, or already cancelled by the student
        return job

    async def _finish(
        self, job: Claimed, state: str, *, result: dict[str, Any] | None = None
    ) -> None:
        now = self.services.clock.now()
        async with self.services.db() as session:
            row = await self._locked(session, job.id)
            if row is None:
                await session.rollback()
            else:
                row.state = state
                row.result = result
                row.finished_at = now
                row.locked_by = None
                if state == "cancelled" and row.stage is None:
                    row.stage = "Остановлено"
                await dialogue.project_terminal(session, row, now)
                await append_event(session, job.id, "status", status_data(row, now))
                if state == "done":
                    await append_event(session, job.id, "done", {"result": result})
                await session.commit()
        await self._drop_transient(job)
        diagnostic_event(
            "job.complete",
            state=state,
            outcome=(result or {}).get("status"),
            error_code=(result or {}).get("detail"),
        )

    async def _fail(
        self,
        job: Claimed,
        code: str,
        detail: str,
        status: int,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = self.services.clock.now()
        async with self.services.db() as session:
            row = await self._locked(session, job.id)
            if row is None:
                await session.rollback()
            else:
                body = error_event(row, code, detail, status, extra)
                row.state = "failed"
                stored: dict[str, Any] = {"code": code, "detail": detail, "status": status}
                if extra:
                    stored["extra"] = extra
                row.error = stored
                row.finished_at = now
                row.locked_by = None
                await dialogue.project_terminal(session, row, now)
                await append_event(session, job.id, "status", status_data(row, now))
                await append_event(session, job.id, "error", body)
                await session.commit()
        await self._drop_transient(job)
        diagnostic_event("job.failed", state="failed", error_code=code, status=status)

    async def _requeue(self, job: Claimed, stage: str, *, delay: timedelta = timedelta(0)) -> None:
        now = self.services.clock.now()
        async with self.services.db() as session:
            row = await self._locked(session, job.id)
            if row is None:
                await session.rollback()
                return
            row.state = "queued"
            row.stage = stage[:120]
            row.run_after = now + delay
            row.locked_by = None
            row.locked_at = None
            await append_event(session, job.id, "status", status_data(row, now))
            await session.execute(select(func.pg_notify(CHANNEL_JOBS, job.id)))
            await session.commit()

        diagnostic_event("job.requeued", state="queued", delay_ms=int(delay.total_seconds() * 1000))

    async def _drop_transient(self, job: Claimed) -> None:
        """Secret answers nobody consumed must not outlive their job."""
        async with self.services.db() as session:
            blocks = (
                await session.scalars(
                    select(JobAnswer.block_id).where(
                        JobAnswer.job_id == job.id, JobAnswer.is_secret.is_(True)
                    )
                )
            ).all()
        for block_id in blocks:
            await self.services.vault.delete(job.user_id, block_id, SecretKind.TRANSIENT)

    # --- the loop ---------------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        cfg = self.config
        await self.notifier.start()
        self.notifier.on(CHANNEL_CANCEL, self._on_cancel)
        background = [
            asyncio.create_task(self._heartbeat_loop(), name="job-heartbeat"),
            asyncio.create_task(self._maintenance_loop(), name="job-maintenance"),
            asyncio.create_task(self._scheduler_loop(), name="job-scheduler"),
        ]
        cleanup = getattr(self.services.vault, "run_cleanup_forever", None)
        if cleanup is not None:
            background.append(asyncio.create_task(cleanup(), name="vault-cleanup"))
        log.info("worker %s started (concurrency %d)", self.name, cfg.concurrency)
        try:
            with self.notifier.watch(CHANNEL_JOBS) as wake:
                while not self._stop.is_set():
                    wake.clear()
                    self._slot_free.clear()
                    while len(self._tasks) < cfg.concurrency and not self._stop.is_set():
                        try:
                            job = await self.claim()
                        except Exception:
                            log.exception("claiming a job failed")
                            break
                        if job is None:
                            break
                        task = asyncio.create_task(self.run_job(job), name=f"job-{job.id}")
                        self._tasks[job.id] = task
                        task.add_done_callback(self._on_done)
                    await self._sleep(wake, cfg.poll_seconds)
        finally:
            await self._shutdown(background)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.pop(self._job_of(task), None)
        self._slot_free.set()

    async def _sleep(self, wake: asyncio.Event, seconds: float) -> None:
        waits = [asyncio.create_task(e.wait()) for e in (self._stop, wake, self._slot_free)]
        try:
            await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in waits:
                t.cancel()

    async def _shutdown(self, background: list[asyncio.Task[None]]) -> None:
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        running = list(self._tasks.values())
        if running:
            _, pending = await asyncio.wait(running, timeout=self.config.shutdown_grace)
            for task in pending:  # still busy after the grace period: hand the job back
                self._cancel_reason[self._job_of(task)] = "shutdown"
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await self.notifier.stop()
        log.info("worker %s stopped", self.name)

    def _job_of(self, task: asyncio.Task[None]) -> str:
        return task.get_name().removeprefix("job-")

    def _on_cancel(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            self._cancel_reason[job_id] = "user"
            task.cancel()

    # --- heartbeat, recovery, scheduler -------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_seconds)
            try:
                await self.heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("heartbeat failed")

    async def heartbeat(self) -> None:
        """Shows the jobs of this worker are alive and notices cancellations that missed NOTIFY."""
        ids = list(self._tasks)
        if not ids:
            return
        now = self.services.clock.now()
        async with self.services.db() as session:
            await session.execute(
                update(Job).where(Job.id.in_(ids), Job.locked_by == self.name).values(locked_at=now)
            )
            cancelled = (
                await session.scalars(
                    select(Job.id).where(Job.id.in_(ids), Job.state == "cancelled")
                )
            ).all()
            gone = set(ids) - set(
                (await session.scalars(select(Job.id).where(Job.id.in_(ids)))).all()
            )
            await session.commit()
        for job_id in (*cancelled, *gone):
            self._on_cancel(job_id)

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await self.maintain()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job maintenance failed")
            await asyncio.sleep(self.config.maintenance_seconds)

    async def maintain(self) -> None:
        purge_diagnostics()
        await self.recover_orphans()
        async with self.services.db() as session:
            await purge_finished(session, self.services.clock.now())
            await session.commit()

    async def recover_orphans(self) -> int:
        """Jobs whose worker stopped heartbeating go back to the queue (or fail after 4 runs)."""
        now = self.services.clock.now()
        stale_before = now - self.config.lease
        async with self.services.db() as session:
            rows = (
                await session.scalars(
                    select(Job)
                    .where(
                        Job.state.in_(("running", "waiting_user")),
                        Job.locked_at.is_not(None),
                        Job.locked_at < stale_before,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for job in rows:
                if job.attempts > len(RETRY_DELAYS):
                    detail = "Задание прервано несколько раз подряд"
                    job.state = "failed"
                    job.error = {"code": "internal_error", "detail": detail, "status": 500}
                    job.finished_at = now
                    job.locked_by = None
                    await dialogue.project_terminal(session, job, now)
                    await append_event(session, job.id, "status", status_data(job, now))
                    await append_event(
                        session,
                        job.id,
                        "error",
                        error_event(job, "internal_error", detail, 500),
                    )
                else:
                    job.state = "queued"
                    job.stage = "Продолжаю после сбоя"
                    job.run_after = now
                    job.locked_by = None
                    job.locked_at = None
                    await append_event(session, job.id, "status", status_data(job, now))
                    await session.execute(select(func.pg_notify(CHANNEL_JOBS, job.id)))
            await session.commit()
            return len(rows)

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await schedule_syncs(self.services, limit=self.config.scheduler_batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sync scheduler failed")
            await asyncio.sleep(self.config.scheduler_seconds)


async def schedule_syncs(
    services: Services, *, limit: int = 100, now: datetime | None = None
) -> int:
    """One scheduler tick: a `sync` job for every live connection whose `next_sync_at` has come.

    Deduplicated by the queue, so every worker may run it and a connection whose job is still
    queued or running gets no second one. Who sets `next_sync_at` is the sync handler (W3)."""
    now = now or services.clock.now()
    async with services.db() as session:
        due = (
            await session.execute(
                select(Connection.id, Connection.user_id)
                .join(User, User.id == Connection.user_id)
                .where(
                    Connection.status.in_(SYNC_STATUSES),
                    Connection.next_sync_at.is_not(None),
                    Connection.next_sync_at <= now,
                    User.disabled.is_(False),
                )
                .order_by(Connection.next_sync_at)
                .limit(limit)
            )
        ).all()
    for connection_id, user_id in due:
        await services.queue.enqueue(
            JobKind.SYNC, user_id=user_id, connection_id=connection_id, dedupe=True
        )
    return len(due)


async def run_forever(
    handlers: HandlerTable,
    *,
    services: Services | None = None,
    config: RunnerConfig | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    if services is None:
        from botai.wiring import build_services

        services = build_services()
    worker = Worker(handlers, services, config=config)
    relay: asyncio.Task[None] | None = None
    if stop is not None:
        relay = asyncio.create_task(_relay(stop, worker))
    else:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        if relay is not None:
            relay.cancel()


async def _relay(stop: asyncio.Event, worker: Worker) -> None:
    await stop.wait()
    worker.stop()
