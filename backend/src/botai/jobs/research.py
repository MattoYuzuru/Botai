"""Private checkpoint persistence: claim lock → checkpoint CAS → Vault cipher, one transaction.

The research engine validates its versioned envelope; this layer sees only a bounded SecretStr.
It never copies plaintext to Job payload/result, events, diagnostics or the normal secrets table.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.clock import Clock
from botai.core.execution import GuardedJobContext
from botai.core.research import ResearchCheckpoint, ResearchConflict, ResearchFormatError
from botai.core.vault import CipherVault, SealedSecret, SecretKind
from botai.db.models import Job, JobResearchCheckpoint

FORMAT_VERSION = 1
MAX_PAYLOAD_BYTES = 512 * 1024
DEFAULT_TTL = timedelta(hours=24)
_INTENT_KEY = re.compile(r"[0-9a-f]{64}")


class ResearchStore:
    def __init__(
        self,
        db: async_sessionmaker[AsyncSession],
        vault: CipherVault,
        clock: Clock,
        *,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("research checkpoint TTL must be positive")
        self._db = db
        self._vault = vault
        self._clock = clock
        self._ttl = ttl

    async def load(self, ctx: GuardedJobContext) -> ResearchCheckpoint | None:
        async with self._db() as session, session.begin():
            await ctx.assert_current(session)
            await self._check_job(session, ctx)
            row = await self._row(session, ctx)
            if row is None:
                return None
            if row.expires_at <= self._clock.now():
                await session.delete(row)
                return None
            self._check_format(row)
            sealed = SealedSecret(
                row.kek_id, row.wrapped_dek, row.nonce, row.ciphertext, row.aad_version
            )
            payload = self._vault.open_value(
                ctx.user_id, self._scope(row), SecretKind.RESEARCH, sealed
            )
            self._payload_value(payload)
            return ResearchCheckpoint(row.revision, row.intent_key, payload)

    async def save(
        self,
        ctx: GuardedJobContext,
        payload: SecretStr,
        *,
        intent_key: str,
        expected_revision: int,
    ) -> ResearchCheckpoint:
        value = self._payload_value(payload)
        if not _INTENT_KEY.fullmatch(intent_key):
            raise ValueError("research intent key must be a 64-character lowercase hex digest")
        if expected_revision < 0:
            raise ValueError("research expected revision must not be negative")
        async with self._db() as session, session.begin():
            await ctx.assert_current(session, input_revision=ctx.input_revision)
            job = await self._check_job(session, ctx)
            row = await self._row(session, ctx)
            now = self._clock.now()
            if row is not None and row.expires_at <= now:
                await session.delete(row)
                await session.flush()
                row = None
            if row is not None:
                self._check_format(row)
            actual_revision = row.revision if row is not None else 0
            if actual_revision != expected_revision:
                raise ResearchConflict("research checkpoint revision changed")
            revision = actual_revision + 1
            if row is None:
                assert ctx.connection_id is not None  # verified by _check_job
                row = JobResearchCheckpoint(
                    job_id=ctx.job_id,
                    user_id=ctx.user_id,
                    connection_id=ctx.connection_id,
                )
            row.revision = revision
            row.format_version = FORMAT_VERSION
            row.intent_key = intent_key
            sealed = self._vault.seal_value(
                ctx.user_id, self._scope(row), SecretKind.RESEARCH, value
            )
            row.kek_id = sealed.kek_id
            row.wrapped_dek = sealed.wrapped_dek
            row.nonce = sealed.nonce
            row.ciphertext = sealed.ciphertext
            row.aad_version = sealed.aad_version
            row.updated_at = now
            row.expires_at = now + self._ttl
            session.add(row)
            # Routing ownership is published atomically with the first durable checkpoint.
            job.payload = {**job.payload, "_research_engine": FORMAT_VERSION}
        return ResearchCheckpoint(revision, intent_key, payload)

    @staticmethod
    def _payload_value(payload: SecretStr) -> str:
        value = payload.get_secret_value()
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            raise ValueError("research checkpoint must be valid UTF-8") from None
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError("research checkpoint exceeds 512 KiB")
        return value

    @staticmethod
    async def _check_job(session: AsyncSession, ctx: GuardedJobContext) -> Job:
        if ctx.connection_id is None:
            raise ResearchConflict("research checkpoint requires a connection")
        job = await session.scalar(
            select(Job).where(Job.id == ctx.job_id, Job.user_id == ctx.user_id)
        )
        if job is None or job.connection_id != ctx.connection_id:
            raise ResearchConflict("research checkpoint does not belong to the current job scope")
        if "_research_engine" in job.payload:
            engine = job.payload["_research_engine"]
            if type(engine) is not int or engine != FORMAT_VERSION:
                raise ResearchFormatError("unsupported research engine version")
        return job

    @staticmethod
    async def _row(session: AsyncSession, ctx: GuardedJobContext) -> JobResearchCheckpoint | None:
        row = await session.scalar(
            select(JobResearchCheckpoint)
            .where(
                JobResearchCheckpoint.job_id == ctx.job_id,
                JobResearchCheckpoint.user_id == ctx.user_id,
            )
            .with_for_update()
        )
        if row is not None and row.connection_id != ctx.connection_id:
            raise ResearchConflict("research checkpoint does not belong to the current connection")
        return row

    @staticmethod
    def _check_format(row: JobResearchCheckpoint) -> None:
        if row.format_version != FORMAT_VERSION:
            raise ResearchFormatError("unsupported research checkpoint format")

    @staticmethod
    def _scope(row: JobResearchCheckpoint) -> str:
        return (
            f"research:{row.job_id}:{row.connection_id}:v{row.format_version}"
            f":r{row.revision}:i{row.intent_key}"
        )


async def purge_expired(db: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Worker maintenance: bounded batch from this private table, skip in-flight row locks."""
    async with db() as session, session.begin():
        ids = list(
            await session.scalars(
                select(JobResearchCheckpoint.job_id)
                .where(JobResearchCheckpoint.expires_at <= now)
                .order_by(JobResearchCheckpoint.expires_at, JobResearchCheckpoint.job_id)
                .limit(1000)
                .with_for_update(skip_locked=True)
            )
        )
        if not ids:
            return 0
        result = await session.execute(
            delete(JobResearchCheckpoint).where(JobResearchCheckpoint.job_id.in_(ids))
        )
        return int(getattr(result, "rowcount", 0) or 0)
