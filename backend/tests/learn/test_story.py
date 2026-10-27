"""The whole W3 story on the synthetic university, in one test: connect -> sync -> the university
changes -> the agent repairs -> the data are back, with no model call outside learning/repair."""

from __future__ import annotations

from datetime import timedelta

from botai.core.jobs import JobKind
from botai.db import models as m
from tests.fakeuni import api as fakeuni_api
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.sources import recipes


async def test_connect_sync_break_repair(make_env: EnvFactory) -> None:
    repaired_timetable = recipes.timetable_endpoint(path="/api/v3/schedule")
    env = await make_env(
        [ls.timetable_answer(), ls.deadlines_answer(), ls.answer([repaired_timetable])]
    )

    # 1. a new university: login, observation, authoring, canary, confirmation, first sync
    connected = await env.run()
    assert connected["status"] == "ok"
    assert await env.specs() == [(1, "active")]
    assert await env.count(m.ScheduleEvent) == 4
    assert [r.purpose for r in env.llm.requests] == ["learn", "learn"]

    # 2. later syncs run on the learned spec and never touch the model
    env.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    assert (await env.sync())["status"] == "ok"
    assert len(env.llm.requests) == 2

    # 3. the university moves its timetable: the next sync notices and keeps the old data
    fakeuni_api.STATE.timetable_version = "v3"
    env.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    noticed = await env.sync()
    assert noticed["status"] == "repairing"
    assert await env.count(m.ScheduleEvent) == 4
    (job,) = env.repair_jobs()

    # 4. the repair job re-learns the endpoint, and the student is told in a few lines
    await env.rebrowse()
    repaired = await env.run(JobKind.REPAIR, job["payload"])
    assert repaired["status"] == "repaired"
    assert [r.purpose for r in env.llm.requests] == ["learn", "learn", "repair"]
    assert "`/api/v2/timetable` → `/api/v3/schedule`" in ls.blocks_of(env.ctx, "text")[-1].markdown

    # 5. data are back on the new revision, and the scheduler may sync again
    conn = await env.conn()
    assert (conn.status, conn.spec_revision, conn.error_code) == ("active", 2, None)
    assert await env.count(m.ScheduleEvent) == 4
    env.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    assert (await env.sync())["status"] == "ok"
    assert len(env.llm.requests) == 3
    assert await env.specs() == [(1, "deprecated"), (2, "active")]
