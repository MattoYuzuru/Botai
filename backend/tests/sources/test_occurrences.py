"""Occurrences of a repeating rule leave only when the rule, looked at today, no longer says them.

The synthetic cabinet (`tests/fakeuni/lms.py`) is read by the real recipe adapter and persisted by
the real normalizer. A rule speaks about the days of its horizon; a day outside it is not "gone",
it was not looked at: the horizon slides forward every day and a repair may shorten it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.contracts.specs import RecipeSpec
from botai.db import models as m
from botai.sources import normalize
from botai.sources.adapter import OCCURRENCE_PREFIX
from botai.sources.adapters.recipe import RecipeAdapter
from tests.fakeuni import lms
from tests.learn.test_lms_story import schedule_endpoint
from tests.sources import recipes
from tests.sources.helpers import NOW, make_ctx
from tests.sources.support import Seeded, seed_connection

MSK = ZoneInfo("Europe/Moscow")
DATABASES = "Основы баз данных"  # one lecture, tuesdays at 13:00, term until 20.12
TUESDAYS_OF_THE_TERM = [
    "2026-10-27",
    "2026-11-03",
    "2026-11-10",
    "2026-11-17",
    "2026-11-24",
    "2026-12-01",
    "2026-12-08",
    "2026-12-15",
]


def spec_of(horizon_from: str, days: int) -> RecipeSpec:
    endpoint = schedule_endpoint()
    endpoint["response"]["expand"]["horizon"] = {"from": horizon_from, "days": days}
    data = recipes.recipe_spec([endpoint], require=("scheduleEvents",))
    data["recipe"]["version"] = 2
    return RecipeSpec.model_validate(data)


class Reading:
    """The cabinet read at a moment and stored, as sync does."""

    def __init__(self, db: AsyncSession, app: object, seeded: Seeded) -> None:
        self.db, self.app, self.seeded = db, app, seeded

    async def at(self, moment: datetime, spec: RecipeSpec) -> dict[str, Any]:
        ctx = make_ctx(self.app)
        ctx.now = moment
        result = await RecipeAdapter(spec).fetch(ctx)
        assert result.complete_kinds() == {"scheduleEvents"}
        stats = await normalize.apply_result(
            self.db,
            user_id=self.seeded.user_id,
            connection_id=self.seeded.connection_id,
            result=result,
            now=moment,
            tz=MSK,
        )
        return stats["scheduleEvents"].as_dict()

    async def tuesdays(self) -> list[str]:
        self.db.expire_all()
        rows = await self.db.scalars(
            select(m.ScheduleEvent)
            .where(
                m.ScheduleEvent.connection_id == self.seeded.connection_id,
                m.ScheduleEvent.title == DATABASES,
                m.ScheduleEvent.kind == "lecture",
            )
            .order_by(m.ScheduleEvent.starts_at)
        )
        found = list(rows)
        assert all(row.external_id.startswith(OCCURRENCE_PREFIX) for row in found)
        return [(row.starts_at + timedelta(hours=3)).date().isoformat() for row in found]


@pytest.fixture
async def reading(db: AsyncSession, fakeuni_app: object) -> Reading:
    return Reading(db, fakeuni_app, await seed_connection(db))


FRIDAY = NOW + timedelta(days=3)  # 30.10, 12:00 UTC
SOON = NOW + timedelta(hours=3)  # a later sync: rows of the earlier one are older than it


async def test_a_sliding_horizon_keeps_what_it_slid_past(reading: Reading) -> None:
    ahead = spec_of("{plusDays:7}", 28)
    await reading.at(NOW, ahead)  # the window of 03.11–30.11
    assert await reading.tuesdays() == TUESDAYS_OF_THE_TERM[1:5]
    # three days later the window starts on 06.11: 03.11 is ahead of "now" but behind the window
    stats = await reading.at(FRIDAY, ahead)
    assert stats["missing"] == 0
    assert await reading.tuesdays() == TUESDAYS_OF_THE_TERM[1:6]  # 03.11 kept, 01.12 came in


async def test_a_shortened_horizon_keeps_the_days_it_no_longer_looks_at(reading: Reading) -> None:
    await reading.at(NOW, spec_of("{weekStart}", 90))
    assert await reading.tuesdays() == TUESDAYS_OF_THE_TERM
    # a repair that reads two weeks says nothing about the weeks after them
    stats = await reading.at(SOON, spec_of("{weekStart}", 14))
    assert stats["missing"] == 0
    assert await reading.tuesdays() == TUESDAYS_OF_THE_TERM
    # and one that moves the start forward does not touch the days before it
    await reading.at(SOON, spec_of("{plusDays:14}", 14))
    assert await reading.tuesdays() == TUESDAYS_OF_THE_TERM


async def test_inside_the_horizon_a_rule_that_stops_saying_a_day_takes_it_off(
    reading: Reading,
) -> None:
    wide = spec_of("{weekStart}", 90)
    await reading.at(NOW, wide)
    # the term of the lecture is cut short: 17.11 and after are no longer the rule's days
    for course in lms.STATE.timetables:
        for row in course["eventRows"]:
            if row["calendarEvent"]["calendarEventId"] == "0b6c2f10-6f0e-4c3a-9a51-1f4d3e9b7a03":
                row["calendarEvent"]["schedule"]["endDate"] = "2026-11-10"
    stats = await reading.at(SOON, wide)
    assert stats["missing"] == 5
    assert await reading.tuesdays() == TUESDAYS_OF_THE_TERM[:3]
    # a class that already began is not touched, whatever the rule says now
    later = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert later > NOW
    await reading.at(later, wide)
    assert await reading.tuesdays() == TUESDAYS_OF_THE_TERM[:3]


async def test_a_read_that_did_not_finish_or_is_not_believed_removes_nothing(
    reading: Reading,
) -> None:
    wide = spec_of("{weekStart}", 90)
    await reading.at(NOW, wide)
    before = await reading.tuesdays()
    # the answer is gone: the endpoint is not complete, so the kind may mark nothing
    lms.STATE.timetables_status = 404
    ctx = make_ctx(reading.app)
    result = await RecipeAdapter(wide).fetch(ctx)
    assert result.complete_kinds() == set()
    await normalize.apply_result(
        reading.db,
        user_id=reading.seeded.user_id,
        connection_id=reading.seeded.connection_id,
        result=result,
        now=NOW + timedelta(hours=3),
        tz=MSK,
    )
    assert await reading.tuesdays() == before
    # a complete read that the runner does not believe (`suspicious`) passes no kind to mark
    lms.STATE.timetables_status = None
    lms.STATE.timetables = []
    result = await RecipeAdapter(wide).fetch(ctx)
    await normalize.apply_result(
        reading.db,
        user_id=reading.seeded.user_id,
        connection_id=reading.seeded.connection_id,
        result=result,
        now=NOW + timedelta(hours=3),
        tz=MSK,
        mark_missing=set(),
    )
    assert await reading.tuesdays() == before


async def test_without_a_horizon_there_is_no_window_and_nothing_is_taken_off(
    reading: Reading,
) -> None:
    await reading.at(NOW, spec_of("{weekStart}", 90))
    before = await reading.tuesdays()
    # a recipe that no longer expands rules has no horizon to speak about
    plain = recipes.recipe_spec([recipes.timetable_endpoint()], require=("scheduleEvents",))
    ctx = make_ctx(reading.app)
    result = await RecipeAdapter(RecipeSpec.model_validate(plain)).fetch(ctx)
    assert all(outcome.expanded is None for outcome in result.outcomes)
    await normalize.apply_result(
        reading.db,
        user_id=reading.seeded.user_id,
        connection_id=reading.seeded.connection_id,
        result=result,
        now=NOW + timedelta(hours=3),
        tz=MSK,
    )
    assert await reading.tuesdays() == before
