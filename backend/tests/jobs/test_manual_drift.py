"""Manual CONNECT keeps drift repair in the same guarded job, on real PG/queue."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
import pytest
from sqlalchemy import delete, select

from botai.browser.session import encode_session
from botai.core.browser import SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import InMemoryJobContext, JobKind, JobState
from botai.core.llm import LlmRequest, LlmResponse, ScriptedLlm
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.learn import explorer as explorer_module
from botai.learn.explorer import Explorer
from botai.sources.runner import SyncOutcome, sync_connection
from tests.fakeuni import api as fakeuni_api
from tests.jobs.env import Env
from tests.learn import support as ls
from tests.sources import recipes
from tests.sources.helpers import FAST, session_state
from tests.sources.support import FakeSessions, seed_connection

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
async def cleanup_specs(env: Env) -> AsyncIterator[None]:
    fakeuni_api.reset()
    async with env.sm() as session:
        before = set(await session.scalars(select(m.AdapterSpecRow.id)))
        audit_before = set(await session.scalars(select(m.AuditLog.id)))
    yield
    fakeuni_api.reset()
    async with env.sm() as session:
        # Production deliberately retains global audit history when its owner/spec is
        # deleted. Remove only this test's additions before their references are nulled.
        await session.execute(delete(m.AuditLog).where(m.AuditLog.id.not_in(audit_before)))
        await session.execute(delete(m.User).where(m.User.id.in_(env.users)))
        await session.execute(delete(m.AdapterSpecRow).where(m.AdapterSpecRow.id.not_in(before)))
        await session.commit()


class EntrySessions(FakeSessions):
    async def ensure_for_entry(
        self,
        ctx: object,
        *,
        user_id: str,
        connection_id: str,
        entry_url: str,
        force: bool = False,
        manual: bool = False,
    ) -> SessionState:
        assert manual
        assert force
        return await self.ensure(ctx, user_id=user_id, connection_id=connection_id, force=force)


async def prepare(env: Env, app: Any, mode: str) -> tuple[str, str, str, list[str]]:
    user = (await env.account()).id
    async with env.sm() as session:
        seeded = await seed_connection(
            session,
            user_id=user,
            spec=recipes.recipe_spec([recipes.timetable_endpoint()], require=("scheduleEvents",)),
            with_spec=mode != "explore",
        )
        await session.commit()
    sessions = EntrySessions()
    sessions.vault = env.services.vault
    env.services.sessions = sessions
    await env.services.vault.put(
        user, seeded.connection_id, SecretKind.SESSION, encode_session(session_state())
    )
    if mode != "explore":
        outcome = await sync_connection(
            InMemoryJobContext(user_id=user),
            env.services,
            seeded.connection_id,
            transport=httpx.ASGITransport(app=app),
            retry=FAST,
        )
        assert outcome.status == "ok"
    async with env.sm() as session:
        ids = list(
            await session.scalars(
                select(m.ScheduleEvent.id)
                .where(m.ScheduleEvent.connection_id == seeded.connection_id)
                .order_by(m.ScheduleEvent.id)
            )
        )
        conn = await session.get(m.Connection, seeded.connection_id)
        assert conn is not None
        if mode != "hot":
            conn.adapter, conn.spec_id, conn.spec_revision = None, None, None
            conn.status = "draft"
        await session.commit()
    manual = await env.services.queue.enqueue(
        JobKind.CONNECT,
        user_id=user,
        connection_id=seeded.connection_id,
        payload={"manual_login": True},
    )
    async with env.sm() as session:
        job = await session.get(m.Job, manual)
        assert job is not None
        job.state = "running"
        await session.commit()
    return user, seeded.connection_id, manual, ids


async def check_no_followup(env: Env, connection: str, manual: str) -> m.Connection:
    async with env.sm() as session:
        jobs = list(await session.scalars(select(m.Job).where(m.Job.connection_id == connection)))
        assert [(job.id, job.kind) for job in jobs] == [(manual, "connect")]
        conn = await session.get(m.Connection, connection)
        assert conn is not None
        return conn


async def test_healthy_manual_replay_remains_model_free(env: Env, fakeuni_app: Any) -> None:
    user, connection, manual, _ = await prepare(env, fakeuni_app, "hot")
    llm = ScriptedLlm()
    env.services.llm = llm
    result = await Explorer(
        InMemoryJobContext(user_id=user, job_id=manual, connection_id=connection),
        env.services,
        transport=httpx.ASGITransport(app=fakeuni_app),
        retry=FAST,
    ).connect(connection, {"manual_login": True})
    assert result == {"status": "ok", "login_resumed": True}
    assert llm.requests == []
    conn = await check_no_followup(env, connection, manual)
    assert conn.status == "active"
    assert conn.data_scope is None


@pytest.mark.parametrize("mode", ["hot", "reuse", "explore"])
async def test_manual_connect_repairs_drift_inline_for_every_final_sync_path(
    env: Env,
    fakeuni_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["hot", "reuse", "explore"],
) -> None:
    user, connection, manual, previous = await prepare(env, fakeuni_app, mode)
    if mode != "explore":
        fakeuni_api.STATE.timetable_version = "v3"
    env.services.browser = await ls.fake_site(fakeuni_app)
    moved = recipes.timetable_endpoint(path="/api/v3/schedule")
    llm = ls.EvidenceLlm(
        queue=([ls.answer([recipes.timetable_endpoint()])] if mode == "explore" else [])
        + [ls.answer([moved])]
    )
    env.services.llm = llm
    queue_flags: list[bool] = []

    async def observed_sync(*args: Any, **kwargs: Any) -> SyncOutcome:
        queue_flags.append(kwargs["queue_repair"])
        if mode == "explore":
            # The fixture moves between successful canary and the first saved-recipe sync.
            fakeuni_api.STATE.timetable_version = "v3"
            env.services.browser = await ls.fake_site(fakeuni_app)
        return await sync_connection(*args, **kwargs)

    monkeypatch.setattr(explorer_module, "sync_connection", observed_sync)
    ctx = InMemoryJobContext(
        user_id=user, job_id=manual, connection_id=connection, responder=lambda block: True
    )
    result = await Explorer(
        ctx, env.services, transport=httpx.ASGITransport(app=fakeuni_app), retry=FAST
    ).connect(connection, {"manual_login": True, "kinds": ["scheduleEvents"]})
    assert result["status"] == "repaired"
    assert result["revision"] == 2
    assert result["fixed"] == ["scheduleEvents"]
    if mode == "reuse":
        assert result["reused"] is True
    if mode == "explore":
        assert "sync" not in result
        assert result["initial_sync"]["status"] == "repairing"
    assert queue_flags == [False]
    assert [request.purpose for request in llm.requests] == (
        ["learn", "repair"] if mode == "explore" else ["repair"]
    )
    conn = await check_no_followup(env, connection, manual)
    assert conn.status == "active"
    assert conn.spec_revision == 2
    async with env.sm() as session:
        current = list(
            await session.scalars(
                select(m.ScheduleEvent.id)
                .where(m.ScheduleEvent.connection_id == connection)
                .order_by(m.ScheduleEvent.id)
            )
        )
    assert len(current) == 4
    if previous:
        assert current == previous


@pytest.mark.parametrize("failure", ["provider", "cancel"])
async def test_failed_or_cancelled_inline_repair_leaves_stale_data_and_retry_schedule(
    env: Env, fakeuni_app: Any, failure: str
) -> None:
    user, connection, manual, previous = await prepare(env, fakeuni_app, "hot")
    fakeuni_api.STATE.timetable_version = "v3"
    env.services.browser = await ls.fake_site(fakeuni_app)

    class Down(ScriptedLlm):
        async def complete(self, request: LlmRequest) -> LlmResponse:
            raise BotaiError(ErrorCode.LLM_UNAVAILABLE, "Synthetic provider unavailable")

    class Context(InMemoryJobContext):
        async def status(self, stage: str, *, state: JobState = JobState.RUNNING) -> None:
            if failure == "cancel" and stage == "Проверяю сайт":
                raise asyncio.CancelledError
            await super().status(stage, state=state)

    env.services.llm = Down()
    explorer = Explorer(
        Context(user_id=user, job_id=manual, connection_id=connection),
        env.services,
        transport=httpx.ASGITransport(app=fakeuni_app),
        retry=FAST,
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await explorer.connect(connection, {"manual_login": True})
    else:
        result = await explorer.connect(connection, {"manual_login": True})
        assert result["status"] != "repairing"
    conn = await check_no_followup(env, connection, manual)
    assert conn.status == "stale"
    assert conn.next_sync_at is not None
    assert conn.data_scope is None
    async with env.sm() as session:
        current = list(
            await session.scalars(
                select(m.ScheduleEvent.id)
                .where(m.ScheduleEvent.connection_id == connection)
                .order_by(m.ScheduleEvent.id)
            )
        )
    assert current == previous
