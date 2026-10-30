"""Sync end to end: fakeuni -> adapter -> Normalizer -> Postgres, with the job handler as entry."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.browser import SessionState
from botai.core.clock import FrozenClock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.execution import student_is_needed
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext, JobKind
from botai.core.queue import InMemoryJobQueue
from botai.core.vault import InMemoryVault, SecretKind
from botai.db import models as m
from botai.sources.runner import make_sync_handler
from botai.sources.session import dump_session
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import data, moodle
from tests.fakeuni import ical as fakeuni_ical
from tests.sources import recipes
from tests.sources.helpers import FAST, NOW, session_state
from tests.sources.support import FakeSessions, Seeded, make_services, seed_connection

PRESET = {
    "expired": {"redirectPathPrefix": ["/auth/"], "probe401": ["/api/v2/me"]},
}


class Env:
    def __init__(
        self,
        db: AsyncSession,
        sm: async_sessionmaker[AsyncSession],
        app: object,
        seeded: Seeded,
        **services: Any,
    ) -> None:
        self.db, self.sm, self.app, self.seeded = db, sm, app, seeded
        self.transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        self.services = make_services(sm, **services)
        self.requests: list[str] = []

    async def login(self, token_state: Any = None) -> None:
        state = token_state or session_state()
        await self.services.vault.put(
            self.seeded.user_id,
            self.seeded.connection_id,
            SecretKind.SESSION,
            dump_session(state),
        )

    async def sync(self, *, user_id: str | None = None) -> dict[str, Any]:
        handler = make_sync_handler(transport=self.transport, retry=FAST)
        ctx = InMemoryJobContext(
            user_id=user_id or self.seeded.user_id, connection_id=self.seeded.connection_id
        )
        result = await handler(ctx, self.services, {})
        assert result is not None
        self.last_ctx = ctx
        return result

    async def conn(self) -> dict[str, Any]:
        self.db.expire_all()
        row = (
            await self.db.execute(
                select(m.Connection).where(m.Connection.id == self.seeded.connection_id)
            )
        ).scalar_one()
        return {
            c: getattr(row, c)
            for c in (
                "status",
                "last_sync_at",
                "next_sync_at",
                "error_code",
                "error_detail",
                "spec_id",
                "spec_revision",
            )
        }

    async def rows(self, model: Any, *cols: str) -> list[tuple[Any, ...]]:
        self.db.expire_all()
        attrs = [getattr(model, c) for c in cols]
        res = await self.db.execute(
            select(*attrs)
            .where(model.connection_id == self.seeded.connection_id)
            .order_by(*attrs[:1])
        )
        return [tuple(r) for r in res.all()]


@pytest.fixture
async def env(db: AsyncSession, sm: async_sessionmaker[AsyncSession], fakeuni_app: object) -> Env:
    seeded = await seed_connection(db, preset=PRESET)
    e = Env(db, sm, fakeuni_app, seeded, sessions=FakeSessions())
    await e.login()
    return e


async def test_first_sync_stores_the_snapshot_with_provenance(env: Env) -> None:
    result = await env.sync()
    assert result["status"] == "ok"
    assert result["kinds"]["scheduleEvents"]["inserted"] == 4
    assert result["kinds"]["deadlines"]["inserted"] == 2

    events = await env.rows(
        m.ScheduleEvent, "external_id", "title", "kind", "starts_at", "ends_at", "location_text"
    )
    assert [e[0] for e in events] == ["ev-1", "ev-2", "ev-3", "ev-4"]
    assert events[0][1:3] == ("Математический анализ", "lecture")
    assert events[0][3] == data.WEEK_START + timedelta(hours=6, minutes=30)
    assert events[0][5] == "А-101"

    prov = await env.rows(
        m.ScheduleEvent, "external_id", "source_url", "fetched_at", "seen_at", "spec_revision"
    )
    assert all(p[1].startswith("https://fakeuni.test/api/v2/timetable") for p in prov)
    assert {p[2] for p in prov} == {NOW} == {p[3] for p in prov}
    assert {p[4] for p in prov} == {1}

    deadlines = await env.rows(m.Deadline, "external_id", "title", "due_at", "state")
    assert [d[0] for d in deadlines] == ["t-1", "t-2"]
    assert deadlines[0][3] == "open"

    conn = await env.conn()
    assert conn["status"] == "active"
    assert conn["last_sync_at"] == NOW
    assert conn["error_code"] is None
    assert conn["spec_revision"] == 1
    # 180 minutes by the recipe's freshness, plus a bounded per-connection jitter
    assert NOW + timedelta(minutes=180) <= conn["next_sync_at"] <= NOW + timedelta(minutes=198)
    assert env.services.llm.requests == []  # type: ignore[attr-defined]  # FR-011: no model in sync
    assert ("Обновляю данные", "running") in [(s, st.value) for s, st in env.last_ctx.statuses]


async def test_every_row_belongs_to_the_owner(env: Env) -> None:
    await env.sync()
    users = await env.rows(m.ScheduleEvent, "user_id")
    assert {u[0] for u in users} == {env.seeded.user_id}


async def test_scope_is_rechecked_before_persisting_an_older_inflight_fetch(env: Env) -> None:
    inner = env.transport

    class NarrowDuringFetch(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await inner.handle_async_request(request)
            if request.url.path == "/api/v2/tasks/deadlines":
                async with env.services.db() as db, db.begin():
                    row = await db.get(m.Connection, env.seeded.connection_id)
                    assert row is not None
                    row.data_scope = ["scheduleEvents"]
            return response

    env.transport = NarrowDuringFetch()
    result = await env.sync()
    assert result["status"] == "ok"
    assert set(result["kinds"]) == {"scheduleEvents"}
    assert len(await env.rows(m.ScheduleEvent, "id")) == 4
    assert await env.rows(m.Deadline, "id") == []


async def test_second_sync_creates_no_duplicates(env: Env) -> None:
    await env.sync()
    first = await env.rows(m.ScheduleEvent, "external_id", "id")
    env.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    again = await env.sync()
    assert again["kinds"]["scheduleEvents"] == {
        "inserted": 0,
        "updated": 0,
        "unchanged": 4,
        "invalid": 0,
        "missing": 0,
    }
    assert await env.rows(m.ScheduleEvent, "external_id", "id") == first
    seen = await env.rows(m.ScheduleEvent, "seen_at")
    assert {s[0] for s in seen} == {NOW + timedelta(hours=3)}
    total = (await env.db.execute(select(func.count()).select_from(m.Deadline))).scalar_one()
    assert total == 2


async def test_a_moved_class_is_updated_and_flagged(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.sync()
    moved = data.timetable()
    moved[2]["place"] = {"room": "Г-1"}  # ev-3, a class still ahead
    monkeypatch.setattr(data, "timetable", lambda: moved)
    result = await env.sync()
    assert result["kinds"]["scheduleEvents"]["updated"] == 1
    rows = await env.rows(m.ScheduleEvent, "external_id", "location_text", "changed")
    assert rows == [
        ("ev-1", "А-101", False),
        ("ev-2", "Б-204", False),
        ("ev-3", "Г-1", True),
        ("ev-4", "online", False),
    ]


async def test_vanished_rows_are_marked_never_deleted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.sync()
    env.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    remaining = [e for e in data.timetable() if e["uid"] not in {"ev-1", "ev-3"}]
    monkeypatch.setattr(data, "timetable", lambda: remaining)
    result = await env.sync()
    assert result["kinds"]["scheduleEvents"]["missing"] == 2
    rows = await env.rows(m.ScheduleEvent, "external_id", "changed", "seen_at")
    assert [r[0] for r in rows] == ["ev-1", "ev-2", "ev-3", "ev-4"]  # nothing deleted
    flags = {r[0]: r[1] for r in rows}
    # ev-3 was still ahead and disappeared: moved or cancelled. ev-1 was already past.
    assert flags == {"ev-1": False, "ev-2": False, "ev-3": True, "ev-4": False}
    assert {r[0]: r[2] for r in rows}["ev-3"] == NOW  # its last sighting stays


async def test_transport_failure_keeps_the_snapshot_and_reports_for_backoff(env: Env) -> None:
    await env.sync()
    before = await env.rows(m.ScheduleEvent, "external_id", "title")
    env.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    fakeuni_api.STATE.fail_status = 503
    with pytest.raises(BotaiError) as exc:
        await env.sync()
    assert exc.value.code == ErrorCode.SOURCE_UNAVAILABLE
    assert await env.rows(m.ScheduleEvent, "external_id", "title") == before
    conn = await env.conn()
    assert conn["status"] == "active"  # old data is still fresh enough
    assert conn["error_code"] == "source_unavailable"
    assert conn["last_sync_at"] == NOW
    assert conn["next_sync_at"] == NOW + timedelta(hours=3, minutes=5)


async def test_long_outage_turns_the_connection_stale(env: Env) -> None:
    await env.sync()
    env.services.clock.advance(timedelta(hours=13))  # type: ignore[attr-defined]  # > 720 min
    fakeuni_api.STATE.fail_status = 503
    with pytest.raises(BotaiError):
        await env.sync()
    assert (await env.conn())["status"] == "stale"


async def test_dead_session_logs_in_again_once_and_succeeds(env: Env) -> None:
    fakeuni_api.expire_sessions()
    result = await env.sync()
    assert result["status"] == "ok"
    assert env.services.sessions.calls == 1  # type: ignore[attr-defined]
    assert env.services.sessions.forced == [True]  # type: ignore[attr-defined]
    assert (await env.conn())["status"] == "active"
    assert len(await env.rows(m.ScheduleEvent, "external_id")) == 4


async def test_session_still_rejected_after_login_needs_the_user_not_a_loop(env: Env) -> None:
    fakeuni_api.expire_sessions()
    env.services.sessions = FakeSessions(works=False)  # type: ignore[assignment]
    result = await env.sync()
    assert result["status"] == "needs_reauth"
    assert env.services.sessions.calls == 1  # type: ignore[attr-defined]
    conn = await env.conn()
    assert conn["status"] == "needs_reauth"
    assert conn["error_code"] == "source_auth_required"
    assert conn["next_sync_at"] is None  # the scheduler stops polling until a new login


@pytest.mark.parametrize(
    "code", [ErrorCode.LOGIN_FAILED, ErrorCode.SOURCE_BOT_PROTECTION, ErrorCode.LOGIN_GUARD_REFUSED]
)
async def test_login_that_cannot_be_automated_stops_without_retries(
    env: Env, code: ErrorCode
) -> None:
    fakeuni_api.expire_sessions()
    env.services.sessions = FakeSessions(fail=BotaiError(code, "нужен человек"))  # type: ignore[assignment]
    result = await env.sync()
    assert result["status"] == "needs_reauth"
    conn = await env.conn()
    assert conn["error_code"] == code.value
    assert conn["next_sync_at"] is None
    assert env.services.sessions.calls == 1  # type: ignore[attr-defined]


class WorkerContext(InMemoryJobContext):
    """What a claimed job in the worker offers: a row to move and a claim to check."""

    input_revision = 0

    async def assert_current(
        self, session: AsyncSession, *, input_revision: int | None = None
    ) -> None:
        return None


async def bound_sync_job(env: Env, *, thread_id: str | None) -> tuple[str, WorkerContext]:
    """A scheduled sync as the worker holds it: a running job row, with or without a conversation."""
    job = m.Job(
        id=new_id(),
        user_id=env.seeded.user_id,
        kind="sync",
        state="running",
        connection_id=env.seeded.connection_id,
        thread_id=thread_id,
        payload={},
        run_after=NOW,
    )
    env.db.add(job)
    await env.db.commit()
    ctx = WorkerContext(
        user_id=env.seeded.user_id, connection_id=env.seeded.connection_id, job_id=job.id
    )
    return job.id, ctx


async def real_thread(env: Env) -> str:
    """A conversation that exists: a job can only point at one."""
    thread = m.ChatThread(user_id=env.seeded.user_id, title="Чат")
    env.db.add(thread)
    await env.db.commit()
    return thread.id


async def job_thread(env: Env, job_id: str) -> str | None:
    env.db.expire_all()
    return await env.db.scalar(select(m.Job.thread_id).where(m.Job.id == job_id))


class AskingSessions(FakeSessions):
    """A login that needs the student (a code, a push approval) before it succeeds."""

    async def ensure(
        self, ctx: object, *, user_id: str, connection_id: str, force: bool = False
    ) -> SessionState:
        await student_is_needed()
        return await super().ensure(ctx, user_id=user_id, connection_id=connection_id, force=force)


class Threads:
    """The conversation lookup of the source, injected: it is the conversations' owner's business."""

    def __init__(self, thread_id: str | None) -> None:
        self.thread_id = thread_id
        self.asked = 0

    async def __call__(self, db: AsyncSession, user_id: str, connection_id: str) -> str | None:
        self.asked += 1
        return self.thread_id


async def sync_with(env: Env, ctx: InMemoryJobContext, threads: Threads) -> dict[str, Any]:
    handler = make_sync_handler(transport=env.transport, retry=FAST, find_thread=threads)
    return await handler(ctx, env.services, {})


async def test_a_login_that_needs_the_student_moves_the_job_to_its_sources_conversation(
    env: Env,
) -> None:
    """The question must appear where the student looks for this source."""
    env.services.sessions = AskingSessions()  # type: ignore[assignment]
    env.services.sessions.vault = env.services.vault  # type: ignore[attr-defined]
    threads = Threads(await real_thread(env))
    job_id, ctx = await bound_sync_job(env, thread_id=None)
    fakeuni_api.expire_sessions()
    assert (await sync_with(env, ctx, threads))["status"] == "ok"
    assert await job_thread(env, job_id) == threads.thread_id
    assert threads.asked == 1


async def test_without_a_finder_the_job_goes_to_the_thread_of_its_source_or_a_new_one(
    env: Env,
) -> None:
    """The login of a scheduled sync must appear somewhere the student looks, even for a source
    that was never talked about: its thread is made, and the next sync finds the same one."""
    env.services.sessions = AskingSessions()  # type: ignore[assignment]
    env.services.sessions.vault = env.services.vault  # type: ignore[attr-defined]
    handler = make_sync_handler(transport=env.transport, retry=FAST)
    job_id, ctx = await bound_sync_job(env, thread_id=None)
    fakeuni_api.expire_sessions()
    assert (await handler(ctx, env.services, {}))["status"] == "ok"
    made = await job_thread(env, job_id)
    assert made is not None
    row = await env.db.get(m.ChatThread, made)
    assert row is not None
    assert row.user_id == env.seeded.user_id
    assert row.title == (await env.db.get(m.Connection, env.seeded.connection_id)).title  # type: ignore[union-attr]
    again, ctx_again = await bound_sync_job(env, thread_id=None)
    fakeuni_api.expire_sessions()
    assert (await handler(ctx_again, env.services, {}))["status"] == "ok"
    assert await job_thread(env, again) == made


async def test_a_silent_login_from_the_stored_password_does_not_borrow_a_conversation(
    env: Env,
) -> None:
    threads = Threads(await real_thread(env))
    job_id, ctx = await bound_sync_job(env, thread_id=None)
    fakeuni_api.expire_sessions()  # logs in again, asking nobody
    assert (await sync_with(env, ctx, threads))["status"] == "ok"
    assert env.services.sessions.calls == 1  # type: ignore[attr-defined]
    assert await job_thread(env, job_id) is None
    assert threads.asked == 0


async def test_a_sync_that_needs_no_login_does_not_borrow_a_conversation(env: Env) -> None:
    threads = Threads(await real_thread(env))
    job_id, ctx = await bound_sync_job(env, thread_id=None)
    assert (await sync_with(env, ctx, threads))["status"] == "ok"
    assert await job_thread(env, job_id) is None
    assert threads.asked == 0


async def test_a_job_that_already_has_a_conversation_keeps_it_through_a_login(env: Env) -> None:
    env.services.sessions = AskingSessions()  # type: ignore[assignment]
    env.services.sessions.vault = env.services.vault  # type: ignore[attr-defined]
    threads = Threads(await real_thread(env))
    own = await real_thread(env)
    job_id, ctx = await bound_sync_job(env, thread_id=own)
    fakeuni_api.expire_sessions()
    await sync_with(env, ctx, threads)
    assert await job_thread(env, job_id) == own
    assert threads.asked == 0


async def test_a_source_without_a_conversation_leaves_the_job_where_it_is(env: Env) -> None:
    env.services.sessions = AskingSessions()  # type: ignore[assignment]
    env.services.sessions.vault = env.services.vault  # type: ignore[attr-defined]
    threads = Threads(None)
    job_id, ctx = await bound_sync_job(env, thread_id=None)
    fakeuni_api.expire_sessions()
    assert (await sync_with(env, ctx, threads))["status"] == "ok"
    assert await job_thread(env, job_id) is None


async def test_session_past_its_expiry_hint_costs_no_request_before_login(env: Env) -> None:
    await env.login(session_state(expires_hint=NOW - timedelta(minutes=1)))
    seen: list[str] = []

    class Spy(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return await env.transport.handle_async_request(request)

    handler = make_sync_handler(transport=Spy(), retry=FAST)
    ctx = InMemoryJobContext(user_id=env.seeded.user_id, connection_id=env.seeded.connection_id)
    # the first attempt sends nothing; after the new login the sync goes through
    await handler(ctx, env.services, {})
    assert env.services.sessions.calls == 1  # type: ignore[attr-defined]
    assert seen
    assert all("relogin" not in u for u in seen)


async def test_a_moved_endpoint_keeps_old_data_and_asks_for_repair(env: Env) -> None:
    await env.sync()
    env.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    before = await env.rows(m.ScheduleEvent, "external_id", "seen_at")
    fakeuni_api.STATE.timetable_version = "v3"
    result = await env.sync()
    assert result["status"] == "repairing"
    assert result["drift"] == [
        {"endpoint": "timetable", "kind": "scheduleEvents", "problem": "not_found"}
    ]
    conn = await env.conn()
    assert conn["status"] == "repairing"
    assert conn["error_code"] == "recipe_drift"
    assert conn["last_sync_at"] == NOW  # still the last good snapshot
    assert await env.rows(m.ScheduleEvent, "external_id", "seen_at") == before  # untouched
    # the healthy kind was refreshed anyway
    assert {r[0] for r in await env.rows(m.Deadline, "seen_at")} == {NOW + timedelta(hours=3)}
    queue: InMemoryJobQueue = env.services.queue  # type: ignore[assignment]
    (job,) = queue.jobs
    assert job["kind"] == JobKind.REPAIR
    assert job["connection_id"] == env.seeded.connection_id
    assert job["payload"]["reason"] == "drift"
    assert "token" not in json.dumps(job["payload"])


async def test_changed_markup_with_data_before_is_suspected_not_wiped(env: Env) -> None:
    seeded = await seed_connection(env.db, spec=recipes.full_recipe(revision=7), preset=PRESET)
    other = Env(env.db, env.sm, env.app, seeded, queue=InMemoryJobQueue())
    await other.login()
    await other.sync()
    assert len(await other.rows(m.NewsItem, "external_id")) == 2
    other.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    fakeuni_api.STATE.news_markup = "v2"  # same list, different markup
    result = await other.sync()
    assert result["status"] == "repairing"
    assert result["drift"][0]["problem"] == "empty"
    assert len(await other.rows(m.NewsItem, "external_id")) == 2  # not marked, not removed


async def test_a_student_with_no_deadlines_is_not_drift(
    db: AsyncSession, sm: async_sessionmaker[AsyncSession]
) -> None:
    seeded = await seed_connection(db, spec=recipes.recipe_spec(min_items=1))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/tasks/deadlines":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"items": data.timetable()})

    e = Env(db, sm, None, seeded)  # type: ignore[arg-type]
    e.transport = httpx.MockTransport(handler)  # type: ignore[assignment]
    await e.login()
    result = await e.sync()
    assert result["status"] == "ok"  # nothing before, nothing now: just an empty student
    assert (await e.conn())["status"] == "active"


async def test_unknown_connection_or_other_users_connection_is_not_found(env: Env) -> None:
    with pytest.raises(BotaiError) as exc:
        await env.sync(user_id="01J00000000000000000OTHER0")
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_disabled_and_learning_connections_are_not_synced(env: Env) -> None:
    for status in ("disabled", "learning", "repairing"):
        await env.db.execute(
            m.Connection.__table__.update()
            .where(m.Connection.id == env.seeded.connection_id)
            .values(status=status)
        )
        result = await env.sync()
        assert result["status"] == "skipped"
    assert await env.rows(m.ScheduleEvent, "external_id") == []


async def test_second_student_of_a_university_reuses_the_learned_spec(
    db: AsyncSession, sm: async_sessionmaker[AsyncSession], fakeuni_app: object
) -> None:
    first = await seed_connection(db)
    second = await seed_connection(db, attach_spec=False)  # no spec of its own yet
    assert second.spec_id == first.spec_id  # same row: the university's learned spec
    e = Env(db, sm, fakeuni_app, second)
    await e.login()
    assert (await e.sync())["status"] == "ok"
    assert (await e.conn())["spec_id"] == first.spec_id


async def test_moodle_and_ical_connections_sync_without_a_browser_session(
    db: AsyncSession, sm: async_sessionmaker[AsyncSession], fakeuni_app: object
) -> None:
    lms = "https://lms.fakeuni.test"
    moodle_spec = {
        "adapter": "moodle_ws",
        "origin": lms,
        "functions": ["core_enrol_get_users_courses", "core_webservice_get_site_info"],
    }
    ical_spec = {"adapter": "ical", "origin": lms}
    vault = InMemoryVault()
    for spec, kind, secret in (
        (moodle_spec, SecretKind.TOKEN, moodle.TOKEN),
        (ical_spec, SecretKind.ICAL_URL, fakeuni_ical.URL),
    ):
        seeded = await seed_connection(
            db, spec=spec, origin=lms, allow_hosts=["lms.fakeuni.test"], kind="lms"
        )
        await vault.put(seeded.user_id, seeded.connection_id, kind, secret)
        e = Env(db, sm, fakeuni_app, seeded, vault=vault)
        result = await e.sync()
        assert result["status"] == "ok", spec["adapter"]
        assert (await e.conn())["last_sync_at"] == NOW
    assert e.services.sessions.calls == 0  # type: ignore[attr-defined]


async def test_dead_token_asks_the_user_without_trying_a_browser_login(
    db: AsyncSession, sm: async_sessionmaker[AsyncSession], fakeuni_app: object
) -> None:
    lms = "https://lms.fakeuni.test"
    spec = {"adapter": "moodle_ws", "origin": lms, "functions": ["core_enrol_get_users_courses"]}
    seeded = await seed_connection(db, spec=spec, origin=lms, allow_hosts=["lms.fakeuni.test"])
    vault = InMemoryVault()
    await vault.put(seeded.user_id, seeded.connection_id, SecretKind.TOKEN, "revoked")
    e = Env(db, sm, fakeuni_app, seeded, vault=vault)
    result = await e.sync()
    assert result["status"] == "needs_reauth"
    assert e.services.sessions.calls == 0  # type: ignore[attr-defined]
    assert (await e.conn())["error_code"] == "source_auth_required"


async def test_job_result_and_statuses_carry_no_secrets(env: Env) -> None:
    result = await env.sync()
    dump = json.dumps(result) + repr(env.last_ctx.statuses)
    assert "tok-fixture-session" not in dump
    assert data.PASSWORD not in dump


def test_clock_is_frozen_for_these_tests() -> None:
    assert isinstance(FrozenClock(NOW), FrozenClock)
