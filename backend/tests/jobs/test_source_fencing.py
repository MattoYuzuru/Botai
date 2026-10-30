"""Persistent source writes reject late claims and unread input at their commit boundary."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import delete, func, select, update

from botai.browser.presets import PgLoginStore
from botai.contracts.agent_ui import (
    ConfirmBlock,
    InputBlock,
    SummaryBlock,
    SummaryGroup,
    SummaryItem,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.core.execution import save_secret
from botai.core.ids import new_id
from botai.core.jobs import JobClaimLost, JobInputChanged, JobKind
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.jobs import dialogue
from botai.jobs.context import PgJobContext
from botai.jobs.events import append_event
from botai.jobs.runner import Claimed, Worker
from botai.learn import canary, revisions
from botai.learn.explorer import Explorer
from botai.learn.progress import digest
from botai.sources import runner as source_runner
from botai.sources.adapter import AdapterResult, EndpointOutcome, Record, SyncContext
from botai.sources.runner import (
    ConnInfo,
    Snapshot,
    SpecPin,
    build_context,
    commit_result,
    load_conn_info,
    load_snapshot,
    set_connection,
)
from tests.jobs.env import Env
from tests.jobs.test_fencing import context
from tests.sources import recipes

pytestmark = pytest.mark.db


class InertStage:
    async def finish(self, *args: object, **kwargs: object) -> None:
        pass


class InertUi:
    async def stage(self, *args: object, **kwargs: object) -> InertStage:
        return InertStage()


@dataclass
class Source:
    env: Env
    client: httpx.AsyncClient
    ctx: PgJobContext
    origin: str
    thread_id: str
    spec: dict[str, Any]
    preset: dict[str, Any]
    info: ConnInfo
    snap: Snapshot
    worker: Worker
    claim: Claimed

    def fetched(self, external_id: str = "late") -> AdapterResult:
        return AdapterResult(
            records=[
                Record(
                    "scheduleEvents",
                    external_id,
                    {
                        "title": "Сохранённое занятие"
                        if external_id == "kept"
                        else "Поздний ответ",
                        "startsAt": (self.env.clock.now() + timedelta(days=1)).isoformat(),
                        "endsAt": (self.env.clock.now() + timedelta(days=1, hours=1)).isoformat(),
                    },
                    self.origin + "/api/v2/timetable",
                )
            ],
            fetched_at=self.env.clock.now(),
            spec_revision=1,
            outcomes=[EndpointOutcome("timetable", "scheduleEvents", True, 1, 1)],
        )

    async def new_input(self) -> None:
        response = await self.client.post(
            f"/api/v1/chat/threads/{self.thread_id}/messages",
            json={
                "text": "Новая область: только дедлайны",
                "targetJobId": self.ctx.job_id,
                "clientMessageId": new_id(),
            },
        )
        assert response.status_code == 202

    async def unchanged(self, *, status: str = "active") -> None:
        async with self.env.sm() as db:
            connection = await db.get(m.Connection, self.info.connection_id)
            assert connection is not None
            assert connection.status == status
            assert connection.spec_id == self.snap.spec_id
            assert connection.spec_revision == 1
            assert connection.last_sync_at == self.snap.last_sync_at
            records = list(
                await db.scalars(
                    select(m.ScheduleEvent).where(
                        m.ScheduleEvent.connection_id == self.info.connection_id
                    )
                )
            )
            assert [(r.external_id, r.title, r.changed) for r in records] == [
                ("kept", "Сохранённое занятие", False)
            ]
            assert list(
                await db.execute(
                    select(m.AdapterSpecRow.revision, m.AdapterSpecRow.state).where(
                        m.AdapterSpecRow.origin == self.origin
                    )
                )
            ) == [(1, "active")]
            assert list(
                await db.execute(
                    select(m.LoginPresetRow.revision, m.LoginPresetRow.state).where(
                        m.LoginPresetRow.origin == self.origin
                    )
                )
            ) == [(1, "active")]
        session = await self.env.services.vault.get(
            self.ctx.user_id, self.info.connection_id, SecretKind.SESSION
        )
        assert session is not None
        assert session.get_secret_value() == "current-source-session-fixture"


@pytest.fixture
async def source(env: Env) -> AsyncIterator[Source]:
    client = env.new_client()
    user = (await env.account(client)).id
    origin = f"https://{new_id().lower()}.fakeuni.test"
    host = urlsplit(origin).hostname
    assert host is not None
    space, connection = await env.connection(user, origin=origin)
    spec = recipes.recipe_spec([recipes.timetable_endpoint()], require=("scheduleEvents",))
    spec["recipe"]["origin"] = origin
    preset = {
        "id": "source-fence-fixture",
        "name": "Фикстурный вход",
        "start": {"url": origin + "/auth/login"},
        "allowHosts": [host],
        "mode": "assisted",
        "capture": {"cookiesFor": [host], "bearerFrom": "none"},
    }
    thread_id = new_id()
    async with env.sm() as db, db.begin():
        recipe = m.AdapterSpecRow(
            origin=origin, adapter="recipe", revision=1, state="active", spec=spec
        )
        login = m.LoginPresetRow(origin=origin, revision=1, state="active", preset=preset)
        db.add_all([recipe, login])
        await db.flush()
        # The thread has no purpose: the REPAIR job below ties it to the connection.
        db.add(
            m.ChatThread(id=thread_id, user_id=user, space_id=space, title="Исследование фикстуры")
        )
        row = await db.get(m.Connection, connection)
        assert row is not None
        row.adapter = "recipe"
        row.spec_id = recipe.id
        row.spec_revision = 1
        row.login_preset_id = login.id
        row.allow_hosts = [host]
    job_id = await env.services.queue.enqueue(
        JobKind.REPAIR,
        user_id=user,
        connection_id=connection,
        thread_id=thread_id,
        payload={"_research_engine": 1},
    )
    worker = env.worker({}, name="source-writer")
    claim = await worker.claim()
    assert claim is not None
    assert claim.id == job_id
    ctx = context(env, worker, claim)
    async with env.sm() as db:
        snap = await load_snapshot(db, user, connection)
        info = await load_conn_info(db, user, connection)
    value = Source(env, client, ctx, origin, thread_id, spec, preset, info, snap, worker, claim)
    await commit_result(
        env.services,
        snap,
        value.fetched("kept"),
        env.clock.now(),
        accept_empty=True,
        queue_repair=False,
        ctx=ctx,
    )
    await env.services.vault.put(
        user, connection, SecretKind.SESSION, "current-source-session-fixture"
    )
    async with env.sm() as db:
        value.snap = await load_snapshot(db, user, connection)
        value.info = await load_conn_info(db, user, connection)
    try:
        yield value
    finally:
        await cleanup_source_registry(value)


async def cleanup_source_registry(source: Source) -> None:
    """Undo this fixture's shared rows, including audit history deliberately retained in prod."""
    async with source.env.sm() as db, db.begin():
        await db.execute(
            update(m.Connection)
            .where(m.Connection.id == source.info.connection_id)
            .values(spec_id=None, login_preset_id=None)
        )
        # Canary audit has no spec FK, and activation canaries have no user_id. Neither
        # deleting the shared spec nor the environment's user cleanup can cascade to it.
        # This exact origin is unique to the source fixture; preserve every other origin.
        await db.execute(
            delete(m.AuditLog).where(m.AuditLog.detail["origin"].as_string() == source.origin)
        )
        await db.execute(delete(m.AdapterSpecRow).where(m.AdapterSpecRow.origin == source.origin))
        await db.execute(delete(m.LoginPresetRow).where(m.LoginPresetRow.origin == source.origin))


async def test_source_cleanup_removes_its_shared_canary_but_preserves_another_spec(
    source: Source,
) -> None:
    foreign_origin = f"https://{new_id().lower()}.fakeuni.test"
    try:
        async with source.env.sm() as db, db.begin():
            own = await db.get(m.AdapterSpecRow, source.snap.spec_id)
            assert own is not None
            await revisions.activate(db, own, now=source.env.clock.now())
            foreign = await revisions.add_revision(
                db,
                origin=foreign_origin,
                adapter="recipe",
                spec={**source.spec, "recipe": {**source.spec["recipe"], "origin": foreign_origin}},
            )
            await revisions.activate(db, foreign, now=source.env.clock.now())
            foreign_id = foreign.id
        async with source.env.sm() as db:
            assert await db.scalar(
                select(func.count())
                .select_from(m.AuditLog)
                .where(
                    m.AuditLog.kind == revisions.AUDIT_CANARY,
                    m.AuditLog.detail["spec_id"].as_string() == source.snap.spec_id,
                    m.AuditLog.user_id.is_(None),
                )
            )
        await cleanup_source_registry(source)
        async with source.env.sm() as db:
            assert await db.get(m.AdapterSpecRow, source.snap.spec_id) is None
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(m.AuditLog)
                    .where(m.AuditLog.detail["origin"].as_string() == source.origin)
                )
                == 0
            )
            retained = list(
                await db.scalars(
                    select(m.AuditLog).where(
                        m.AuditLog.kind == revisions.AUDIT_CANARY,
                        m.AuditLog.detail["spec_id"].as_string() == foreign_id,
                    )
                )
            )
            assert [entry.detail["via"] for entry in retained] == ["activation"]
            assert await db.get(m.AdapterSpecRow, foreign_id) is not None
    finally:
        async with source.env.sm() as db, db.begin():
            await db.execute(
                delete(m.AuditLog).where(m.AuditLog.detail["origin"].as_string() == foreign_origin)
            )
            await db.execute(
                delete(m.AdapterSpecRow).where(m.AdapterSpecRow.origin == foreign_origin)
            )


@pytest.mark.parametrize("write", ["status", "snapshot", "activate", "session", "preset", "host"])
async def test_late_claim_cannot_mutate_source_or_shared_recipe(
    source: Source, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    source.env.advance(minutes=3)
    survivor = source.env.worker({}, name="new-source-owner")
    assert await survivor.recover_orphans() == 1
    assert await survivor.claim() is not None
    explorer = Explorer(source.ctx, source.env.services)
    # A failed UI emit would hide a missing guard in the actual activation transaction.
    monkeypatch.setattr(explorer, "ui", InertUi())
    store = PgLoginStore(source.env.sm)

    async def late_write() -> None:
        if write == "status":
            await set_connection(
                source.env.services,
                source.info.connection_id,
                source.ctx.user_id,
                status="error",
                ctx=source.ctx,
            )
        elif write == "snapshot":
            await commit_result(
                source.env.services,
                source.snap,
                source.fetched(),
                source.env.clock.now(),
                accept_empty=True,
                queue_repair=False,
                ctx=source.ctx,
            )
        elif write == "activate":
            await explorer._activate(source.info, source.spec, {}, None, "fixture", True)
        elif write == "session":
            await save_secret(
                source.ctx,
                source.env.services.vault,
                source.ctx.user_id,
                source.info.connection_id,
                SecretKind.SESSION,
                "old-claim-session-fixture",
            )
        elif write == "preset":
            await store.save_preset(
                source.origin,
                source.preset,
                connection_id=source.info.connection_id,
                ctx=source.ctx,
            )
        else:
            await store.allow_host(
                source.ctx.user_id,
                source.info.connection_id,
                "additional.fakeuni.test",
                ctx=source.ctx,
            )

    with pytest.raises(JobClaimLost):
        await late_write()
    await source.unchanged()
    async with source.env.sm() as db:
        row = await db.get(m.Connection, source.info.connection_id)
        assert row is not None
        assert row.allow_hosts == [urlsplit(source.origin).hostname]


async def test_new_message_during_canary_prevents_activation_of_its_old_result(
    source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedAdapter:
        kind = "recipe"

        async def fetch(self, ctx: SyncContext) -> AdapterResult:
            started.set()
            await release.wait()
            return source.fetched()

    def delayed_adapter(_self: object, _spec: dict[str, Any]) -> DelayedAdapter:
        return DelayedAdapter()

    monkeypatch.setattr(canary.AdapterFactory, "create", delayed_adapter)
    explorer = Explorer(source.ctx, source.env.services)
    monkeypatch.setattr(explorer, "ui", InertUi())
    sctx = build_context(source.env.services, source.snap, None)
    reading = asyncio.create_task(explorer._canary(source.spec, sctx, require=["scheduleEvents"]))
    try:
        async with asyncio.timeout(2):
            await started.wait()
        await source.new_input()
        release.set()
        report = await reading
        assert report.ok
        with pytest.raises(JobInputChanged):
            await explorer._activate(source.info, source.spec, {}, None, "fixture", True)
        assert source.ctx.input_revision == 0
        await source.unchanged()
    finally:
        release.set()
        if not reading.done():
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading


async def test_disabled_source_cannot_activate_a_shared_revision(
    source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with source.env.sm() as db, db.begin():
        await db.execute(
            update(m.Connection)
            .where(m.Connection.id == source.info.connection_id)
            .values(status="disabled")
        )
    explorer = Explorer(source.ctx, source.env.services)
    monkeypatch.setattr(explorer, "ui", InertUi())
    result = await explorer._activate(source.info, source.spec, {}, None, "fixture", True)
    assert result == {"status": "skipped", "detail": "disabled"}
    await source.unchanged(status="disabled")


async def test_unread_input_cannot_publish_an_old_summary_or_confirmation(source: Source) -> None:
    await source.new_input()
    assert await source.ctx.load_research() is None
    assert source.ctx.input_revision == 0
    count = len(await source.env.events(source.ctx.job_id))
    summary = SummaryBlock(
        id=new_id(),
        at=source.env.clock.now(),
        title="Старый результат",
        groups=[SummaryGroup(title="Расписание", items=[SummaryItem(label="Записей", value="1")])],
    )
    with pytest.raises(JobInputChanged):
        await source.ctx.emit(summary)
    question = ConfirmBlock(
        id=new_id(),
        at=source.env.clock.now(),
        question="Сохранить старую область?",
        confirm_label="Сохранить",
        cancel_label="Отмена",
    )
    async with asyncio.timeout(2):
        with pytest.raises(JobInputChanged):
            await source.ctx.ask(question)
    assert source.ctx.input_revision == 0
    assert len(await source.env.events(source.ctx.job_id)) == count
    async with source.env.sm() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(m.ChatMessage)
                .where(m.ChatMessage.job_id == source.ctx.job_id, m.ChatMessage.role == "agent")
            )
            == 0
        )
    turns = await source.ctx.drain_dialogue()
    assert len(turns) == 1
    assert source.ctx.input_revision == 1


@pytest.mark.parametrize("protected", [False, True], ids=["old-confirm", "password-input"])
async def test_changed_input_retires_only_the_old_data_question(
    source: Source, protected: bool
) -> None:
    now = source.env.clock.now()
    block = (
        InputBlock(
            id=new_id(),
            at=now,
            question="Пароль",
            input_kind="password",
            autocomplete="current-password",
            secret=True,
            required=True,
        )
        if protected
        else ConfirmBlock(
            id=new_id(),
            at=now,
            question="Сохранить прежние данные?",
            confirm_label="Сохранить",
            cancel_label="Отмена",
        )
    )
    async with source.env.sm() as db, db.begin():
        job = await db.get(m.Job, source.ctx.job_id, with_for_update=True)
        assert job is not None
        dialogue.set_pending(job, block, channel="native", now=now)
        # Exercise the legacy descriptor too: its type is recovered from its own event.
        state = dialogue.state_of(job)
        state.pop("pending_kind", None)
        dialogue.save_state(job, state)
        await append_event(
            db, job.id, "block", block.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        dialogue.advance_input(job)
        job.state = "waiting_user"

    async def changed_input(_ctx: object, _services: object, _payload: dict[str, Any]) -> None:
        raise JobInputChanged

    source.worker.handlers[JobKind.REPAIR] = changed_input
    await source.worker.run_job(source.claim)
    row = await source.env.job(source.ctx.job_id)
    state = dialogue.state_of(row)
    assert row.state == "queued"
    assert row.payload["_retry_failures"] == 0
    if protected:
        assert state["pending"]["block_id"] == block.id
        assert f"block:{block.id}" not in state.get("receipts", {})
    else:
        assert "pending" not in state
        assert state["receipts"][f"block:{block.id}"]["status"] == "superseded"


@pytest.mark.parametrize("after_fetch", [False, True], ids=["before-read", "before-commit"])
async def test_activation_sync_never_switches_its_approved_revision(
    source: Source, monkeypatch: pytest.MonkeyPatch, after_fetch: bool
) -> None:
    pin = SpecPin(source.snap.spec_id, source.snap.spec_revision, digest(source.snap.spec))
    fetched = False

    async def replace_shared_revision() -> None:
        async with source.env.sm() as db, db.begin():
            row = await revisions.add_revision(
                db, origin=source.origin, adapter="recipe", spec=source.spec, state="canary"
            )
            await revisions.activate(db, row, now=source.env.clock.now())

    async def switch_after_fetch(*_args: object, **_kwargs: object) -> AdapterResult:
        nonlocal fetched
        fetched = True
        await replace_shared_revision()
        return source.fetched("late")

    monkeypatch.setattr(source_runner, "fetch_with_reauth", switch_after_fetch)
    if not after_fetch:
        await replace_shared_revision()
        with pytest.raises(BotaiError) as failure:
            await source_runner.sync_connection(
                source.ctx, source.env.services, source.info.connection_id, expected_spec=pin
            )
        assert failure.value.code == ErrorCode.RECIPE_REJECTED
        assert not fetched
    else:
        outcome = await source_runner.sync_connection(
            source.ctx, source.env.services, source.info.connection_id, expected_spec=pin
        )
        assert fetched
        assert outcome.status == "error"
        assert outcome.detail == ErrorCode.RECIPE_REJECTED.value
    async with source.env.sm() as db:
        rows = list(
            await db.scalars(
                select(m.ScheduleEvent).where(
                    m.ScheduleEvent.connection_id == source.info.connection_id
                )
            )
        )
        assert [(row.external_id, row.title) for row in rows] == [("kept", "Сохранённое занятие")]
        connection = await db.get(m.Connection, source.info.connection_id)
        assert connection is not None
        assert connection.spec_id == source.snap.spec_id
        assert connection.spec_revision == source.snap.spec_revision
        assert connection.last_sync_at == source.snap.last_sync_at
