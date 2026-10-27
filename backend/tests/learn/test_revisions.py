"""Revisions, their health, the daily canary and the comparison with the confirmed snapshot."""

from __future__ import annotations

import copy
from collections import Counter
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.jobs import JobKind
from botai.core.queue import InMemoryJobQueue
from botai.db import models as m
from botai.learn import revisions
from botai.learn.canary import compare, keys_of, load_baseline
from botai.sources.adapter import Record
from tests.fakeuni import api as fakeuni_api
from tests.learn.conftest import EnvFactory
from tests.sources import recipes
from tests.sources.helpers import NOW
from tests.sources.support import seed_connection

MSK = ZoneInfo("Europe/Moscow")


# --- lifecycle ------------------------------------------------------------------------------


async def test_add_assigns_the_next_number_and_writes_it_into_the_spec(db: AsyncSession) -> None:
    spec = recipes.recipe_spec(revision=99)
    a = await revisions.add_revision(db, origin="https://o.test", adapter="recipe", spec=spec)
    b = await revisions.add_revision(db, origin="https://o.test", adapter="recipe", spec=spec)
    other = await revisions.add_revision(db, origin="https://p.test", adapter="recipe", spec=spec)
    assert (a.revision, b.revision, other.revision) == (1, 2, 1)
    assert a.spec["recipe"]["revision"] == 1
    assert b.spec["recipe"]["revision"] == 2
    assert spec["recipe"]["revision"] == 99  # the caller's dict is not touched


async def test_only_one_revision_is_active_per_origin_and_adapter(db: AsyncSession) -> None:
    origin = "https://o.test"
    rows = [
        await revisions.add_revision(
            db, origin=origin, adapter="recipe", spec=recipes.recipe_spec()
        )
        for _ in range(3)
    ]
    assert await revisions.activate(db, rows[0]) is None
    assert (await revisions.activate(db, rows[1])).id == rows[0].id  # type: ignore[union-attr]
    assert (await revisions.activate(db, rows[2])).id == rows[1].id  # type: ignore[union-attr]
    states = [r.state for r in rows]
    assert states == ["deprecated", "deprecated", "active"]
    assert (await revisions.active_row(db, origin, "recipe")).id == rows[2].id  # type: ignore[union-attr]
    # activating the already active one changes nothing
    assert await revisions.activate(db, rows[2]) is None
    assert rows[2].state == "active"


async def test_rollback_returns_to_the_previous_revision_and_keeps_history(
    db: AsyncSession,
) -> None:
    origin = "https://o.test"
    old = await revisions.add_revision(
        db, origin=origin, adapter="recipe", spec=recipes.recipe_spec()
    )
    await revisions.activate(db, old)
    new = await revisions.add_revision(
        db, origin=origin, adapter="recipe", spec=recipes.recipe_spec()
    )
    await revisions.activate(db, new)
    back = await revisions.rollback(db, new, now=NOW)
    assert back is not None
    assert back.id == old.id
    assert (old.state, new.state) == ("active", "rolled_back")
    # a revision nobody can fall back to stays: a broken spec is still better than none
    assert await revisions.rollback(db, old) is None
    assert old.state == "active"
    log = (
        (await db.execute(select(m.AuditLog).where(m.AuditLog.kind == "spec_rollback")))
        .scalars()
        .all()
    )
    assert [e.detail["to_revision"] for e in log] == [1]


# --- three drifts in a row ------------------------------------------------------------------


async def two_revisions(make_env: EnvFactory):
    env = await make_env([], with_spec=True)
    await env.login()
    row = (await env.db.execute(select(m.AdapterSpecRow))).scalar_one()
    second = await revisions.add_revision(
        env.db, origin=row.origin, adapter="recipe", spec=copy.deepcopy(row.spec)
    )
    await revisions.activate(env.db, second)
    assert await env.specs() == [(1, "deprecated"), (2, "active")]
    return env


async def test_three_drifts_in_a_row_roll_a_revision_back(make_env: EnvFactory) -> None:
    env = await two_revisions(make_env)
    fakeuni_api.STATE.timetable_version = "v3"
    details = []
    for _ in range(3):
        await env.db.execute(
            m.Connection.__table__.update()
            .where(m.Connection.id == env.seeded.connection_id)
            .values(status="active")
        )
        result = await env.sync()
        assert result["status"] == "repairing"
        details.append(result["detail"])
    assert details == ["", "", "rolled_back"]
    assert await env.specs() == [(1, "active"), (2, "rolled_back")]
    drifts = await env.audit("spec_drift")
    assert len(drifts) == 3


async def test_a_good_sync_between_drifts_resets_the_count(make_env: EnvFactory) -> None:
    env = await two_revisions(make_env)

    async def sync_with(version: str) -> dict[str, str]:
        fakeuni_api.STATE.timetable_version = version
        await env.db.execute(
            m.Connection.__table__.update()
            .where(m.Connection.id == env.seeded.connection_id)
            .values(status="active")
        )
        return await env.sync()

    for version in ("v3", "v3", "v2", "v3", "v3"):
        await sync_with(version)
    assert await env.specs() == [(1, "deprecated"), (2, "active")]  # never three in a row
    assert len(await env.audit("spec_ok")) == 1


# --- the daily canary -----------------------------------------------------------------------


async def test_canary_checks_are_queued_for_the_author_once_a_day(
    db: AsyncSession, sm: async_sessionmaker[AsyncSession]
) -> None:
    author = await seed_connection(db)
    other = await seed_connection(db)
    row = (await db.execute(select(m.AdapterSpecRow))).scalar_one()
    row.learned_by_digest = revisions.author_digest(author.user_id)
    await db.flush()
    queue = InMemoryJobQueue()

    assert await revisions.enqueue_due_canaries(sm, queue, NOW) == 1
    (job,) = queue.jobs
    assert job["kind"] == JobKind.REPAIR
    assert job["connection_id"] == author.connection_id  # on the author's session
    assert job["user_id"] == author.user_id
    assert job["payload"] == {"mode": "check", "reason": "canary"}

    assert await revisions.enqueue_due_canaries(sm, queue, NOW + timedelta(hours=23)) == 0
    assert await revisions.enqueue_due_canaries(sm, queue, NOW + timedelta(hours=25)) == 1
    assert queue.jobs[-1]["connection_id"] == author.connection_id
    assert other.connection_id != author.connection_id


async def test_without_the_author_another_student_is_used_after_two_days(
    db: AsyncSession, sm: async_sessionmaker[AsyncSession]
) -> None:
    gone = await seed_connection(db, status="disabled")
    other = await seed_connection(db)
    row = (await db.execute(select(m.AdapterSpecRow))).scalar_one()
    row.learned_by_digest = revisions.author_digest(gone.user_id)
    await db.flush()
    queue = InMemoryJobQueue()

    assert await revisions.enqueue_due_canaries(sm, queue, NOW) == 1  # nobody checked it yet
    assert queue.jobs[-1]["connection_id"] == other.connection_id
    assert await revisions.enqueue_due_canaries(sm, queue, NOW + timedelta(hours=25)) == 0
    assert await revisions.enqueue_due_canaries(sm, queue, NOW + timedelta(hours=49)) == 1


async def test_a_spec_nobody_uses_is_not_checked(
    db: AsyncSession, sm: async_sessionmaker[AsyncSession]
) -> None:
    await seed_connection(db, status="needs_reauth")
    assert await revisions.enqueue_due_canaries(sm, InMemoryJobQueue(), NOW) == 0


async def test_a_successful_sync_counts_as_a_canary(make_env: EnvFactory) -> None:
    env = await make_env([], with_spec=True)
    await env.login()
    await env.sync()
    assert [c.detail["via"] for c in await env.audit("canary")] == ["sync"]
    assert env.repair_jobs() == []  # so no separate check is queued right after
    await env.sync()
    assert len(await env.audit("canary")) == 1  # throttled: one record per few hours


async def test_connecting_a_university_counts_as_the_first_canary(make_env: EnvFactory) -> None:
    from tests.learn import support as ls

    env = await make_env([ls.timetable_answer(), ls.deadlines_answer()])
    await env.run()
    entries = await env.audit("canary")
    assert entries[0].detail["via"] == "activation"
    assert env.repair_jobs() == []


# --- comparison with the confirmed snapshot -------------------------------------------------


def event(title: str, start: str, ext: str = "x") -> Record:
    return Record(
        "scheduleEvents",
        ext,
        {"title": title, "startsAt": start, "endsAt": start},
        "https://fakeuni.test/p",
    )


def counter(*events: Record) -> Counter[tuple[str, ...]]:
    return keys_of(list(events), "scheduleEvents", MSK)


def test_same_classes_match_whatever_their_ids_are() -> None:
    base = counter(
        event("A", "2026-10-28T10:00:00+00:00", "1"), event("B", "2026-10-28T12:00:00+00:00", "2")
    )
    now = [
        event("A", "2026-10-28T10:00:00+00:00", "new-1"),
        event("B", "2026-10-28T12:00:00+00:00", "new-2"),
    ]
    result = compare("scheduleEvents", base, now, MSK)
    assert (result.verdict, result.matched, result.baseline) == ("match", 2, 2)


def test_a_candidate_reading_other_data_is_a_mismatch() -> None:
    base = counter(event("A", "2026-10-28T10:00:00+00:00"), event("B", "2026-10-28T12:00:00+00:00"))
    wrong = [
        event("room 1", "2026-10-28T10:00:00+00:00"),
        event("room 2", "2026-10-28T12:00:00+00:00"),
    ]
    result = compare("scheduleEvents", base, wrong, MSK)
    assert (result.verdict, result.matched) == ("mismatch", 0)


def test_a_few_changes_since_the_snapshot_are_tolerated() -> None:
    times = [f"2026-10-{20 + i}T10:00:00+00:00" for i in range(1, 6)]
    base = counter(*[event(f"C{i}", t) for i, t in enumerate(times)])
    now = [event(f"C{i}", t) for i, t in enumerate(times[:4])] + [
        event("moved", "2026-11-30T10:00:00+00:00")
    ]
    result = compare("scheduleEvents", base, now, MSK)
    assert (result.verdict, result.matched, result.baseline) == ("match", 4, 5)  # 0.8 >= 0.6


def test_with_nothing_comparable_the_candidate_is_unverified() -> None:
    assert (
        compare("scheduleEvents", None, [event("A", "2026-10-28T10:00:00+00:00")], MSK).verdict
        == "unverified"
    )
    assert compare("scheduleEvents", Counter(), [], MSK).verdict == "unverified"


async def test_baseline_is_the_last_confirmed_snapshot_of_what_is_still_ahead(
    make_env: EnvFactory,
) -> None:
    env = await make_env([], with_spec=True)
    await env.login()
    await env.sync()
    conn = await env.conn()
    baseline = await load_baseline(env.db, env.seeded.connection_id, conn.last_sync_at, NOW)
    # Monday's two classes are past by Tuesday noon: only Wednesday and Friday can prove anything
    assert sum(baseline["scheduleEvents"].values()) == 2
    assert sum(baseline["deadlines"].values()) == 2
    assert await load_baseline(env.db, env.seeded.connection_id, None, NOW) == {}
