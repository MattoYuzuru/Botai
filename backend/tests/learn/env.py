"""An environment for job handlers: services over a rolled-back database, fakeuni on loopback."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.jobs import InMemoryJobContext, JobKind
from botai.core.llm import LlmResponse
from botai.core.queue import InMemoryJobQueue
from botai.core.vault import InMemoryVault, SecretKind
from botai.db import models as m
from botai.learn.handlers import make_handlers
from botai.sources.ingest import make_ingest_handler
from botai.sources.runner import make_sync_handler
from botai.sources.session import dump_session
from tests.fakeuni import data
from tests.learn import support as ls
from tests.sources.helpers import FAST, session_state
from tests.sources.support import FakeSessions, Seeded, make_services

PRESET = {"expired": {"redirectPathPrefix": ["/auth/"]}}


class Env:
    def __init__(
        self,
        db: AsyncSession,
        sm: async_sessionmaker[AsyncSession],
        app: object,
        seeded: Seeded,
        responses: list[LlmResponse],
        browser: Any,
        *,
        sessions: FakeSessions | None = None,
    ) -> None:
        self.db, self.seeded, self.app = db, seeded, app
        self.llm = ls.EvidenceLlm(queue=responses)
        self.vault = InMemoryVault()
        self.sessions = sessions or FakeSessions()
        self.queue = InMemoryJobQueue()
        self.services = make_services(
            sm,
            llm=self.llm,
            vault=self.vault,
            sessions=self.sessions,
            queue=self.queue,
            browser=browser,
        )
        self.transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        self.handlers = make_handlers(transport=self.transport, retry=FAST)
        self.handlers[JobKind.INGEST] = make_ingest_handler(transport=self.transport, retry=FAST)
        self.ctx: InMemoryJobContext
        self.asked: list[Any] = []

    # --- setup ---------------------------------------------------------------------------

    async def prepare(self) -> None:
        for kind, value in (
            (SecretKind.LOGIN, data.USERNAME),
            (SecretKind.PASSWORD, data.PASSWORD),
        ):
            await self.vault.put(self.seeded.user_id, self.seeded.connection_id, kind, value)

    async def login(self) -> None:
        """The student is logged in on fakeuni and the session is in the Vault."""
        await self.vault.put(
            self.seeded.user_id,
            self.seeded.connection_id,
            SecretKind.SESSION,
            dump_session(session_state()),
        )

    async def rebrowse(self) -> None:
        """Let the observing browser see the university as it is now."""
        self.services.browser = await ls.fake_site(self.app)

    # --- running jobs --------------------------------------------------------------------

    async def run(
        self,
        kind: JobKind = JobKind.CONNECT,
        payload: dict[str, Any] | None = None,
        **user: Any,
    ) -> dict[str, Any]:
        self.ctx, self.asked = ls.scripted_user(
            self.seeded.user_id, self.seeded.connection_id, **user
        )
        # Existing stories explicitly request the two original MVP sections. New chat jobs
        # default to schedule only; individual tests pass {} to exercise that behavior.
        requested = payload if payload is not None else {"kinds": ["scheduleEvents", "deadlines"]}
        result = await self.handlers[kind](self.ctx, self.services, requested)
        assert result is not None
        return result

    async def sync(self) -> dict[str, Any]:
        ctx = InMemoryJobContext(
            user_id=self.seeded.user_id, connection_id=self.seeded.connection_id
        )
        handler = make_sync_handler(transport=self.transport, retry=FAST)
        result = await handler(ctx, self.services, {})
        assert result is not None
        return result

    # --- reading state -------------------------------------------------------------------

    async def conn(self) -> m.Connection:
        self.db.expire_all()
        return (
            await self.db.execute(
                select(m.Connection).where(m.Connection.id == self.seeded.connection_id)
            )
        ).scalar_one()

    async def specs(self, origin: str = data.ORIGIN) -> list[tuple[int, str]]:
        self.db.expire_all()
        rows = await self.db.execute(
            select(m.AdapterSpecRow.revision, m.AdapterSpecRow.state)
            .where(m.AdapterSpecRow.origin == origin)
            .order_by(m.AdapterSpecRow.revision)
        )
        return [(r[0], r[1]) for r in rows.all()]

    async def spec(self, revision: int) -> dict[str, Any]:
        self.db.expire_all()
        return (
            await self.db.execute(
                select(m.AdapterSpecRow.spec).where(m.AdapterSpecRow.revision == revision)
            )
        ).scalar_one()

    async def count(self, model: Any) -> int:
        self.db.expire_all()
        return (
            await self.db.execute(
                select(func.count())
                .select_from(model)
                .where(model.connection_id == self.seeded.connection_id)
            )
        ).scalar_one()

    async def chunks(self) -> int:
        """Search fragments of the documents of this connection."""
        self.db.expire_all()
        return (
            await self.db.execute(
                select(func.count())
                .select_from(m.Chunk)
                .join(m.Document, m.Document.id == m.Chunk.document_id)
                .where(m.Document.connection_id == self.seeded.connection_id)
            )
        ).scalar_one()

    async def audit(self, kind: str) -> list[m.AuditLog]:
        self.db.expire_all()
        rows = await self.db.execute(
            select(m.AuditLog).where(m.AuditLog.kind == kind).order_by(m.AuditLog.id)
        )
        return list(rows.scalars())

    async def ids(self, model: Any) -> list[str]:
        self.db.expire_all()
        rows = await self.db.execute(
            select(model.external_id)
            .where(model.connection_id == self.seeded.connection_id)
            .order_by(model.external_id)
        )
        return [r[0] for r in rows.all()]

    async def rows_revision(self) -> set[int]:
        """Spec revisions that wrote the schedule rows."""
        self.db.expire_all()
        rows = await self.db.execute(
            select(m.ScheduleEvent.spec_revision).where(
                m.ScheduleEvent.connection_id == self.seeded.connection_id
            )
        )
        return {r[0] for r in rows.all()}

    def repair_jobs(self) -> list[dict[str, Any]]:
        return [j for j in self.queue.jobs if j["kind"] == JobKind.REPAIR]
