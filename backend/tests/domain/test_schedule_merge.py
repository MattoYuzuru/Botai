"""Several sources of one university show one schedule: the primary decides (FR-054, L1, Postgres).

Stored rows stay per source; the merge happens on every read. The API (`/schedule`, `/today`), the
primary choice, switching a source off and on, and the cross-space conflicts are checked against the
real tables. The pure rules (tolerance, titles, notes) are in `test_merge.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, select

from botai.core.clock import FrozenClock
from botai.db.models import Connection, ScheduleEvent, Space
from tests.auth.support import Sm, signup
from tests.domain.helpers import T0, Campus, add, add_source, at, event

WEEK = {"start": "2026-10-26T00:00:00Z", "end": "2026-11-02T00:00:00Z"}


async def schedule(c: httpx.AsyncClient, **params: str) -> list[dict[str, Any]]:
    r = await c.get("/api/v1/schedule", params={**WEEK, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def choose(c: Campus, connection_id: str | None) -> httpx.Response:
    return await c.client.put(
        f"/api/v1/spaces/{c.space}/primary-source", json={"connectionId": connection_id}
    )


def titles(sources: list[dict[str, Any]]) -> list[str]:
    return [s["title"] for s in sources]


async def seed_one_class(c: Campus, sm: Sm) -> tuple[str, str]:
    """The same class in both sources: the cabinet knows the room, the calendar the teacher."""
    cabinet = event(
        c.user_id,
        c.cabinet,
        at(9),
        at(10, 20),
        title="Алгоритмы",
        kind="lecture",
        location_text="305",
    )
    calendar = event(
        c.user_id,
        c.calendar,
        at(9, 5),
        at(10, 25),
        title="алгоритмы (лекция)",
        teacher_text="Иванов И. И.",
        url="https://meet.fakeuni.test/algo",
        location_text="405",
    )
    await add(sm, cabinet, calendar)
    return cabinet.id, calendar.id


async def test_one_class_from_two_sources_is_one_event(campus: Campus, tx_sessionmaker: Sm) -> None:
    cabinet_id, _ = await seed_one_class(campus, tx_sessionmaker)
    [one] = await schedule(campus.client)
    assert one["id"] == cabinet_id  # the primary's identity
    assert (one["startsAt"], one["endsAt"]) == ("2026-10-27T09:00:00Z", "2026-10-27T10:20:00Z")
    assert one["title"] == "Алгоритмы"
    assert one["kind"] == "lecture"
    assert one["location"] == "305"  # both have it: the primary wins
    assert one["teacher"] == "Иванов И. И."  # only the calendar has it: filled
    assert one["url"] == "https://meet.fakeuni.test/algo"
    assert titles(one["sources"]) == ["Кабинет", "Календарь"]
    assert [s["connectionId"] for s in one["sources"]] == [campus.cabinet, campus.calendar]
    assert one["provenance"]["connectionId"] == campus.cabinet
    assert one["disagreement"]["kind"] == "several"  # room and a start five minutes later
    assert "«Календарь»" in one["disagreement"]["detail"]
    assert "ауд. 405" in one["disagreement"]["detail"]


async def test_a_class_of_one_source_carries_only_that_source(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    await add(tx_sessionmaker, event(campus.user_id, campus.cabinet, at(9), title="Алгоритмы"))
    [one] = await schedule(campus.client)
    assert titles(one["sources"]) == ["Кабинет"]
    assert one["disagreement"] is None


async def test_the_merge_never_changes_what_is_stored(campus: Campus, tx_sessionmaker: Sm) -> None:
    await seed_one_class(campus, tx_sessionmaker)
    await schedule(campus.client)
    async with tx_sessionmaker() as s:
        rows = (await s.scalars(select(ScheduleEvent).order_by(ScheduleEvent.starts_at))).all()
    assert [(r.connection_id, r.location_text, r.teacher_text) for r in rows] == [
        (campus.cabinet, "305", None),
        (campus.calendar, "405", "Иванов И. И."),
    ]


# --- who is the primary -------------------------------------------------------------------------


async def test_by_default_the_oldest_source_with_events_decides(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    ancient = await add_source(
        tx_sessionmaker, campus.user_id, campus.space, "Старый без данных", age_days=30
    )
    await seed_one_class(campus, tx_sessionmaker)
    [one] = await schedule(campus.client)
    assert titles(one["sources"]) == ["Кабинет", "Календарь"]  # not the older source without events
    roles = await roles_of(campus)
    assert roles[campus.cabinet] == "primary"
    assert roles[campus.calendar] == "secondary"
    assert roles[ancient] is None


async def roles_of(campus: Campus) -> dict[str, str | None]:
    r = await campus.client.get(f"/api/v1/spaces/{campus.space}/connections")
    assert r.status_code == 200, r.text
    return {row["id"]: row["scheduleRole"] for row in r.json()}


async def test_when_only_the_newer_source_has_events_it_decides(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    await add(tx_sessionmaker, event(campus.user_id, campus.calendar, at(9), title="Алгоритмы"))
    [one] = await schedule(campus.client)
    assert titles(one["sources"]) == ["Календарь"]
    assert (await roles_of(campus))[campus.calendar] == "primary"


async def test_the_chosen_source_decides_and_the_old_one_fills_in(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    _, calendar_id = await seed_one_class(campus, tx_sessionmaker)
    assert (await choose(campus, campus.calendar)).status_code == 204
    [one] = await schedule(campus.client)
    assert one["id"] == calendar_id
    assert (one["startsAt"], one["endsAt"]) == ("2026-10-27T09:05:00Z", "2026-10-27T10:25:00Z")
    assert one["title"] == "алгоритмы (лекция)"
    assert one["location"] == "405"
    assert one["kind"] == "lecture"  # the calendar does not say, the cabinet does
    assert titles(one["sources"]) == ["Календарь", "Кабинет"]
    assert "ауд. 305" in one["disagreement"]["detail"]
    roles = await roles_of(campus)
    assert (roles[campus.calendar], roles[campus.cabinet]) == ("primary", "secondary")


async def test_the_choice_validates_its_source(campus: Campus, tx_sessionmaker: Sm) -> None:
    other = await choose_foreign(campus)
    assert other.status_code == 404  # a source of another university
    unknown = await choose(campus, "00000000000000000000000001")
    assert unknown.status_code == 404
    off = await campus.client.post(f"/api/v1/connections/{campus.calendar}/disable")
    assert off.status_code == 200
    assert (await choose(campus, campus.calendar)).status_code == 409  # a disabled one cannot
    assert (await choose(campus, campus.cabinet)).status_code == 204
    assert (await choose(campus, None)).status_code == 204  # back to the default
    async with tx_sessionmaker() as s:
        pointer = await s.scalar(
            select(Space.primary_schedule_connection_id).where(Space.id == campus.space)
        )
    assert pointer is None


async def choose_foreign(campus: Campus) -> httpx.Response:
    return await campus.client.put(
        f"/api/v1/spaces/{campus.space}/primary-source", json={"connectionId": campus.elsewhere}
    )


async def test_the_choice_of_a_foreign_space_is_a_404(campus: Campus) -> None:
    r = await campus.client.put(
        "/api/v1/spaces/00000000000000000000000001/primary-source",
        json={"connectionId": campus.cabinet},
    )
    assert r.status_code == 404


# --- switching a source off and on --------------------------------------------------------------


async def test_a_disabled_source_leaves_every_view_and_comes_back(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    await seed_one_class(campus, tx_sessionmaker)
    off = await campus.client.post(f"/api/v1/connections/{campus.cabinet}/disable")
    assert off.status_code == 200
    assert off.json()["status"] == "disabled"
    assert off.json()["scheduleRole"] is None
    [only] = await schedule(campus.client)
    assert titles(only["sources"]) == ["Календарь"]
    assert only["location"] == "405"
    today = await campus.client.get("/api/v1/today")
    assert [titles(e["sources"]) for e in today.json()["events"]] == [["Календарь"]]
    on = await campus.client.post(f"/api/v1/connections/{campus.cabinet}/enable")
    assert on.status_code == 200
    assert on.json()["status"] == "active"
    assert on.json()["nextSyncAt"] is not None  # it syncs at the next tick
    [back] = await schedule(campus.client)
    assert titles(back["sources"]) == ["Кабинет", "Календарь"]
    async with tx_sessionmaker() as s:
        assert (
            await s.scalar(select(func.count()).select_from(ScheduleEvent)) == 2
        )  # nothing deleted


async def test_switching_is_idempotent_and_an_unfinished_source_goes_back_to_draft(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    c = campus.client
    for _ in range(2):
        assert (await c.post(f"/api/v1/connections/{campus.cabinet}/disable")).json()[
            "status"
        ] == "disabled"
    async with tx_sessionmaker() as s:
        row = await s.get(Connection, campus.cabinet)
        assert row is not None
        row.adapter = None  # connecting never finished
        await s.commit()
    back = await c.post(f"/api/v1/connections/{campus.cabinet}/enable")
    assert back.json()["status"] == "draft"
    assert back.json()["nextSyncAt"] is None
    again = await c.post(f"/api/v1/connections/{campus.cabinet}/enable")
    assert again.json()["status"] == "draft"


async def test_a_disabled_primary_falls_back_and_the_choice_applies_again_when_it_is_back(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    await seed_one_class(campus, tx_sessionmaker)
    await choose(campus, campus.calendar)
    await campus.client.post(f"/api/v1/connections/{campus.calendar}/disable")
    [alone] = await schedule(campus.client)
    assert titles(alone["sources"]) == ["Кабинет"]
    assert (await roles_of(campus))[campus.cabinet] == "primary"
    await campus.client.post(f"/api/v1/connections/{campus.calendar}/enable")
    [again] = await schedule(campus.client)
    assert titles(again["sources"]) == ["Календарь", "Кабинет"]


async def test_a_deleted_primary_clears_the_choice(campus: Campus, tx_sessionmaker: Sm) -> None:
    await seed_one_class(campus, tx_sessionmaker)
    await choose(campus, campus.calendar)
    assert (await campus.client.delete(f"/api/v1/connections/{campus.calendar}")).status_code == 204
    async with tx_sessionmaker() as s:
        pointer = await s.scalar(
            select(Space.primary_schedule_connection_id).where(Space.id == campus.space)
        )
    assert pointer is None
    [alone] = await schedule(campus.client)
    assert titles(alone["sources"]) == ["Кабинет"]


async def test_deleting_a_university_with_a_chosen_primary_works(
    campus: Campus,
) -> None:
    await choose(campus, campus.cabinet)
    assert (await campus.client.delete(f"/api/v1/spaces/{campus.space}")).status_code == 204


async def test_another_user_cannot_switch_my_source(
    campus: Campus,
    new_client: Callable[[], httpx.AsyncClient],
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    bob = new_client()
    await signup(bob, tx_sessionmaker, clock, email="bob@example.com")
    for action in ("disable", "enable"):
        r = await bob.post(f"/api/v1/connections/{campus.cabinet}/{action}")
        assert r.status_code == 404
    r = await bob.put(
        f"/api/v1/spaces/{campus.space}/primary-source", json={"connectionId": campus.cabinet}
    )
    assert r.status_code == 404
    async with tx_sessionmaker() as s:
        status = await s.scalar(select(Connection.status).where(Connection.id == campus.cabinet))
    assert status == "active"


# --- a class the primary lacks, universities, conflicts -----------------------------------------


async def test_a_class_missing_from_the_primary_is_marked_with_data_outside_the_window(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    """The cabinet has the class five days earlier (outside the window asked for) and not on the day:
    the calendar's class on the day is shown, with a note."""
    await add(
        tx_sessionmaker,
        event(campus.user_id, campus.cabinet, at(9, day=22), title="Алгоритмы"),
        event(campus.user_id, campus.cabinet, at(9, day=30), title="Алгоритмы"),
        event(campus.user_id, campus.calendar, at(9, day=27), title="Алгоритмы"),
    )
    [one] = await schedule(campus.client, start="2026-10-27T00:00:00Z", end="2026-10-28T00:00:00Z")
    assert titles(one["sources"]) == ["Календарь"]
    assert one["disagreement"]["kind"] == "not_in_primary"
    assert "«Кабинет»" in one["disagreement"]["detail"]


async def test_nothing_is_marked_beyond_the_reach_of_the_primary(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    await add(
        tx_sessionmaker,
        event(campus.user_id, campus.cabinet, at(9, day=22), title="Алгоритмы"),
        event(campus.user_id, campus.calendar, at(9, day=27), title="Алгоритмы"),
    )
    [one] = await schedule(campus.client, start="2026-10-27T00:00:00Z", end="2026-10-28T00:00:00Z")
    assert (
        one["disagreement"] is None
    )  # the cabinet's last class ended before this one: no data there


async def test_universities_never_merge(campus: Campus, tx_sessionmaker: Sm) -> None:
    await add(
        tx_sessionmaker,
        event(campus.user_id, campus.cabinet, at(9), title="Алгоритмы", location_text="305"),
        event(campus.user_id, campus.elsewhere, at(9), title="Алгоритмы", location_text="12"),
    )
    got = await schedule(campus.client)
    assert len(got) == 2
    assert {tuple(titles(e["sources"])) for e in got} == {("Кабинет",), ("Расписание Б",)}
    assert all(e["disagreement"] is None for e in got)
    by_space = {e["provenance"]["spaceId"]: e for e in got}
    assert by_space[campus.space]["conflictsWith"] == [by_space[campus.other_space]["id"]]


async def test_cross_university_conflicts_use_the_merged_class(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    cabinet_id, _ = await seed_one_class(campus, tx_sessionmaker)
    there = event(campus.user_id, campus.elsewhere, at(9, 30), at(10, 30), title="Йога")
    await add(tx_sessionmaker, there)
    got = {e["title"]: e for e in await schedule(campus.client)}
    assert got["Алгоритмы"]["conflictsWith"] == [
        there.id
    ]  # once, though two sources have the class
    assert got["Йога"]["conflictsWith"] == [cabinet_id]
    assert len(got) == 2


async def test_the_same_university_is_never_a_conflict_with_itself(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    await add(
        tx_sessionmaker,
        event(campus.user_id, campus.cabinet, at(9), at(10, 30), title="Алгоритмы"),
        event(campus.user_id, campus.calendar, at(9, 30), at(11), title="Йога"),
    )
    assert all(e["conflictsWith"] == [] for e in await schedule(campus.client))


async def test_today_merges_like_the_schedule(campus: Campus, tx_sessionmaker: Sm) -> None:
    await seed_one_class(campus, tx_sessionmaker)
    r = await campus.client.get("/api/v1/today")
    assert r.status_code == 200, r.text
    [one] = r.json()["events"]
    assert titles(one["sources"]) == ["Кабинет", "Календарь"]
    assert one["teacher"] == "Иванов И. И."


async def test_three_months_of_two_sources_merge_through_the_api(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    rows = []
    for d in range(90):
        start = at(7) + timedelta(days=d)
        rows.append(
            event(campus.user_id, campus.cabinet, start, title="Алгоритмы", location_text="305")
        )
        rows.append(
            event(
                campus.user_id,
                campus.calendar,
                start + timedelta(minutes=5),
                start + timedelta(minutes=85),
                title="Алгоритмы",
                teacher_text="Иванов",
            )
        )
    await add(tx_sessionmaker, *rows)
    r = await campus.client.get(
        "/api/v1/schedule",
        params={"start": T0.isoformat(), "end": (T0 + timedelta(days=95)).isoformat()},
    )
    assert r.status_code == 200
    got = r.json()
    assert len(got) == 89  # the class of the first day ended before the window began
    assert all(len(e["sources"]) == 2 for e in got)
