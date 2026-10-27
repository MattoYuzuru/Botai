"""PgJobContext: what a job handler talks to the student through (core.jobs.JobContext).

Every call is a short transaction against `jobs` / `job_events` / `job_answers`; nothing is held
while waiting. Waiting for an answer sleeps on a NOTIFY wake-up with a polling fallback, ends in
`JobCancelled` when the job is cancelled or nobody answers within the TTL (30 minutes), and never
logs or stores the answer of a secret input: that value lives in the Vault (`transient`, keyed by
the block id) from the API call until `ask_secret` consumes and deletes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import timedelta

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.contracts.agent_ui import AgentUiAnswer, AgentUiBlock, InputBlock
from botai.core.clock import Clock
from botai.core.jobs import JobCancelled, JobState
from botai.core.vault import SecretKind, Vault
from botai.db.models import Job, JobAnswer
from botai.jobs.events import TERMINAL, append_event, status_data
from botai.jobs.notify import CHANNEL_ANSWERS, Notifier

log = logging.getLogger(__name__)

ASK_TTL = timedelta(minutes=30)
POLL_SECONDS = 2.0
STAGE_MAX = 120


class PgJobContext:
    def __init__(
        self,
        *,
        job_id: str,
        user_id: str,
        connection_id: str | None,
        db: async_sessionmaker[AsyncSession],
        vault: Vault,
        clock: Clock,
        notifier: Notifier,
        ask_ttl: timedelta = ASK_TTL,
        poll_seconds: float = POLL_SECONDS,
        on_wait_start: Callable[[], None] | None = None,
        on_wait_end: Callable[[], None] | None = None,
    ) -> None:
        self.job_id = job_id
        self.user_id = user_id
        self.connection_id = connection_id
        self._db = db
        self._vault = vault
        self._clock = clock
        self._notifier = notifier
        self._ask_ttl = ask_ttl
        self._poll = poll_seconds
        self._on_wait_start = on_wait_start
        self._on_wait_end = on_wait_end
        self._waiting_via_status = False

    # --- JobContext -------------------------------------------------------------------------

    async def status(self, stage: str, *, state: JobState = JobState.RUNNING) -> None:
        if state not in (JobState.RUNNING, JobState.WAITING_USER):
            raise ValueError("a handler may only report running or waiting_user")
        async with self._db() as session:
            job = await self._live_job(session)
            job.stage = stage[:STAGE_MAX]
            job.state = state.value
            await append_event(session, self.job_id, "status", status_data(job, self._clock.now()))
            await session.commit()
        # A handler that reports `waiting_user` itself (a human step on the fallback screen) is
        # waiting for the student too: that time is not part of the job's budget.
        waiting = state is JobState.WAITING_USER
        if waiting and not self._waiting_via_status and self._on_wait_start is not None:
            self._on_wait_start()
        if not waiting and self._waiting_via_status and self._on_wait_end is not None:
            self._on_wait_end()
        self._waiting_via_status = waiting

    async def emit(self, block: AgentUiBlock) -> None:
        """Idempotent by `block.id`: emitting the id again replaces the block on the client."""
        async with self._db() as session:
            await self._live_job(session)
            await append_event(session, self.job_id, "block", _dump(block))
            await session.commit()

    async def ask(self, block: AgentUiBlock) -> AgentUiAnswer:
        if isinstance(block, InputBlock) and block.secret:
            raise ValueError("use ask_secret for secret inputs")
        answer = await self._ask(block)
        return AgentUiAnswer(block_id=block.id, values=answer.values)

    async def ask_secret(self, block: InputBlock) -> SecretStr:
        if not block.secret:
            raise ValueError("ask_secret requires InputBlock(secret=True)")
        answer = await self._ask(block)
        if not answer.is_secret:
            raise TypeError("a secret input was answered without the secret path")
        secret = await self._vault.get(self.user_id, block.id, SecretKind.TRANSIENT)
        if secret is None:
            raise JobCancelled(f"the secret answer to block {block.id} is gone (expired)")
        await self._vault.delete(self.user_id, block.id, SecretKind.TRANSIENT)  # read once
        return secret

    # --- internals --------------------------------------------------------------------------

    async def _live_job(self, session: AsyncSession) -> Job:
        """The locked job row, or `JobCancelled` if it ended or was deleted under the handler."""
        job = await session.scalar(select(Job).where(Job.id == self.job_id).with_for_update())
        if job is None or job.state in TERMINAL:
            raise JobCancelled(f"job {self.job_id} is no longer running")
        return job

    async def _ask(self, block: AgentUiBlock) -> JobAnswer:
        async with self._db() as session:
            job = await self._live_job(session)
            await append_event(session, self.job_id, "block", _dump(block))
            job.state = JobState.WAITING_USER.value
            await append_event(session, self.job_id, "status", status_data(job, self._clock.now()))
            await session.commit()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ask_ttl.total_seconds()
        if self._on_wait_start is not None:
            self._on_wait_start()  # the student's thinking time is not the job's time
        try:
            with self._notifier.watch(CHANNEL_ANSWERS, self.job_id) as wake:
                while True:
                    wake.clear()
                    answer = await self._answer_or_cancel(block.id)
                    if answer is not None:
                        break
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        await self._expire()
                        raise JobCancelled(f"nobody answered block {block.id} in time")
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(wake.wait(), min(self._poll, remaining))
        finally:
            if self._on_wait_end is not None:
                self._on_wait_end()
        await self._resume()
        return answer

    async def _answer_or_cancel(self, block_id: str) -> JobAnswer | None:
        async with self._db() as session:
            job = await session.get(Job, self.job_id)
            if job is None or job.state in TERMINAL:
                raise JobCancelled(f"job {self.job_id} is no longer running")
            return await session.scalar(
                select(JobAnswer).where(
                    JobAnswer.job_id == self.job_id, JobAnswer.block_id == block_id
                )
            )

    async def _resume(self) -> None:
        async with self._db() as session:
            job = await self._live_job(session)
            job.state = JobState.RUNNING.value
            await append_event(session, self.job_id, "status", status_data(job, self._clock.now()))
            await session.commit()

    async def _expire(self) -> None:
        """The wait TTL ran out: the job is cancelled, its progress (result so far) is kept."""
        async with self._db() as session:
            job = await session.scalar(select(Job).where(Job.id == self.job_id).with_for_update())
            if job is None or job.state in TERMINAL:
                return
            now = self._clock.now()
            job.state = JobState.CANCELLED.value
            job.stage = "Не дождались ответа, задание остановлено"
            job.finished_at = now
            job.locked_by = None
            await append_event(session, self.job_id, "status", status_data(job, now))
            await session.commit()


def _dump(block: AgentUiBlock) -> dict[str, object]:
    return block.model_dump(mode="json", by_alias=True)
