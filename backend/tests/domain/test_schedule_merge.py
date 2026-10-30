"""Several sources of one university show one schedule: the primary decides (FR-054, L1, Postgres).

Stored rows stay per source; the merge happens on every read. The API (`/schedule`, `/today`), the
primary choice, switching a source off and on, and the cross-space conflicts are checked against the
real tables. The pure rules (tolerance, titles, notes) are in `test_merge.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

from botai.core.clock import FrozenClock
from botai.core.ids import new_id
from botai.db.models import ChatThread, Connection, Job, ScheduleEvent, Space
from tests.auth.support import Sm, signup
from tests.domain.helpers import (
    T0,
    Campus,
    add,
    add_source,
    at,
    course,
    deadline,
    document,
    event,
    grade,
    news,
    person,
)

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


@pytest.mark.parametrize("status", ["needs_reauth", "error"])
async def test_by_default_a_source_that_needs_the_student_does_not_decide_but_a_choice_stands(
    campus: Campus, tx_sessionmaker: Sm, status: str
) -> None:
    await seed_one_class(campus, tx_sessionmaker)
    async with tx_sessionmaker() as s:
        row = await s.get(Connection, campus.cabinet)
        assert row is not None
        row.status = status
        await s.commit()
    [one] = await schedule(campus.client)
    assert titles(one["sources"]) == [
        "Календарь",
        "Кабинет",
    ]  # the healthy one first, though younger
    roles = await roles_of(campus)
    assert (roles[campus.calendar], roles[campus.cabinet]) == ("primary", "secondary")
    # the student's own choice is respected whatever the state of the source
    assert (await choose(campus, campus.cabinet)).status_code == 204
    [chosen] = await schedule(campus.client)
    assert titles(chosen["sources"]) == ["Кабинет", "Календарь"]
    assert (await roles_of(campus))[campus.cabinet] == "primary"
    # the one that was passed over is a healthy source again: the oldest decides, as before
    await choose(campus, None)
    async with tx_sessionmaker() as s:
        row = await s.get(Connection, campus.cabinet)
        assert row is not None
        row.status = "stale"
        await s.commit()
    [back] = await schedule(campus.client)
    assert titles(back["sources"]) == ["Кабинет", "Календарь"]


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


async def seed_records(c: Campus, sm: Sm) -> None:
    """Every kind of record the cabinet has, a document of it, and a document that belongs to no
    source (a file of a chat): the first leave with the source, the last stays."""
    due = T0 + timedelta(days=3)
    await add(
        sm,
        deadline(c.user_id, c.cabinet, due, title="Сдать лабораторную"),
        course(c.user_id, c.cabinet, "Базы данных"),
        grade(c.user_id, c.cabinet, "9"),
        person(c.user_id, c.cabinet, "Сидорова Е. Н."),
        news(c.user_id, c.cabinet, T0, title="Новость кабинета"),
    )
    texts = [
        document(c.user_id, c.space, c.cabinet, "Правила кабинета", "пересдача проходит в среду"),
        document(c.user_id, c.space, None, "Файл из чата", "памятка первокурснику"),
    ]
    await add(sm, *(doc for doc, _ in texts))  # a chunk needs its document in the table first
    await add(sm, *(chunk for _, chunk in texts))


async def records(c: Campus) -> dict[str, int]:
    """How many records of each kind the student sees in the university of the campus."""
    client, space = c.client, c.space
    out: dict[str, int] = {}
    for kind in ("deadlines", "courses", "grades", "people", "news"):
        r = await client.get(f"/api/v1/{kind}", params={"space_id": space})
        assert r.status_code == 200, r.text
        out[kind] = len(r.json())
    docs = await client.get(f"/api/v1/spaces/{space}/documents")
    assert docs.status_code == 200, docs.text
    out["documents"] = len(docs.json())
    sections = await client.get(f"/api/v1/spaces/{space}/sections")
    out["documentsSection"] = next(
        (row["count"] for row in sections.json() if row["kind"] == "documents"), 0
    )
    return out


async def test_a_disabled_source_leaves_every_view_and_comes_back(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    await seed_one_class(campus, tx_sessionmaker)
    await seed_records(campus, tx_sessionmaker)
    full = {
        "deadlines": 1,
        "courses": 1,
        "grades": 1,
        "people": 1,
        "news": 1,
        "documents": 2,
        "documentsSection": 2,
    }
    assert await records(campus) == full
    off = await campus.client.post(f"/api/v1/connections/{campus.cabinet}/disable")
    assert off.status_code == 200
    assert off.json()["status"] == "disabled"
    assert off.json()["scheduleRole"] is None
    [only] = await schedule(campus.client)
    assert titles(only["sources"]) == ["Календарь"]
    assert only["location"] == "405"
    today = await campus.client.get("/api/v1/today")
    assert [titles(e["sources"]) for e in today.json()["events"]] == [["Календарь"]]
    assert today.json()["deadlines"] == []
    # nothing of the cabinet is shown or counted; the file that belongs to no source stays
    assert await records(campus) == {
        "deadlines": 0,
        "courses": 0,
        "grades": 0,
        "people": 0,
        "news": 0,
        "documents": 1,
        "documentsSection": 1,
    }
    on = await campus.client.post(f"/api/v1/connections/{campus.cabinet}/enable")
    assert on.status_code == 200
    assert on.json()["status"] == "active"
    assert on.json()["nextSyncAt"] is not None  # it syncs at the next tick
    [back] = await schedule(campus.client)
    assert titles(back["sources"]) == ["Кабинет", "Календарь"]
    assert await records(campus) == full
    async with tx_sessionmaker() as s:
        assert (
            await s.scalar(select(func.count()).select_from(ScheduleEvent)) == 2
        )  # nothing deleted


@pytest.mark.parametrize(
    ("status", "adapter", "synced"),
    [
        ("active", "recipe", True),
        ("stale", "recipe", True),
        ("needs_reauth", "recipe", False),  # still needs the student to log in
        ("error", "recipe", False),
        ("draft", None, False),  # connecting never finished
        ("draft", "ical", False),  # a calendar that waits for its link still waits
    ],
)
async def test_switching_on_returns_the_source_to_the_status_it_had(
    campus: Campus, tx_sessionmaker: Sm, status: str, adapter: str | None, synced: bool
) -> None:
    async with tx_sessionmaker() as s:
        row = await s.get(Connection, campus.cabinet)
        assert row is not None
        row.status, row.adapter = status, adapter
        await s.commit()
    c = campus.client
    for _ in range(2):  # switching off twice changes nothing the second time
        off = await c.post(f"/api/v1/connections/{campus.cabinet}/disable")
        assert off.json()["status"] == "disabled"
    for _ in range(2):
        on = await c.post(f"/api/v1/connections/{campus.cabinet}/enable")
        assert on.json()["status"] == status
        assert (on.json()["nextSyncAt"] is not None) is synced
    async with tx_sessionmaker() as s:
        row = await s.get(Connection, campus.cabinet)
        assert row is not None
        assert row.disabled_from is None


async def test_switching_off_stops_the_tasks_of_the_source_and_the_thread_stays_free(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    """A task that waits for an answer would swallow the next message of its thread and refuse it
    for a source that is off: the task ends with the source, and the message goes to the agent."""
    thread = ChatThread(id=new_id(), user_id=campus.user_id, space_id=campus.space)
    waiting = Job(
        id=new_id(),
        user_id=campus.user_id,
        kind="connect",
        state="waiting_user",
        connection_id=campus.cabinet,
        thread_id=thread.id,
        run_after=T0,
        payload={"dialogue": {"pending": {"channel": "dialogue", "block_id": new_id()}}},
    )
    running = Job(
        id=new_id(),
        user_id=campus.user_id,
        kind="sync",
        state="running",
        connection_id=campus.cabinet,
        run_after=T0,
    )
    elsewhere = Job(
        id=new_id(),
        user_id=campus.user_id,
        kind="sync",
        state="queued",
        connection_id=campus.calendar,
        run_after=T0,
    )
    async with tx_sessionmaker() as s:
        row = await s.get(Connection, campus.cabinet)
        assert row is not None
        row.status = "learning"
        s.add(thread)
        await s.flush()
        s.add_all([waiting, running, elsewhere])
        await s.commit()
    listed = await campus.client.get(f"/api/v1/spaces/{campus.space}/connections")
    assert next(r for r in listed.json() if r["id"] == campus.cabinet)["activeTask"] is not None
    off = await campus.client.post(f"/api/v1/connections/{campus.cabinet}/disable")
    assert off.status_code == 200
    assert off.json()["activeTask"] is None
    async with tx_sessionmaker() as s:
        states = {
            j.id: j.state for j in await s.scalars(select(Job).where(Job.user_id == campus.user_id))
        }
    assert states[waiting.id] == "cancelled"
    assert states[running.id] == "cancelled"
    assert states[elsewhere.id] == "queued"  # another source's task is not touched
    # the thread no longer waits for an answer for the source that was switched off
    runtime = await campus.client.get(f"/api/v1/chat/threads/{thread.id}/runtime")
    assert runtime.status_code == 200, runtime.text
    assert runtime.json()["pending"] == []
    assert [t["state"] for t in runtime.json()["tasks"]] == ["cancelled"]
    assert runtime.json()["activeJob"] is None
    sent = await campus.client.post(
        f"/api/v1/chat/threads/{thread.id}/messages", json={"text": "что у меня завтра?"}
    )
    assert sent.status_code == 202, sent.text
    async with tx_sessionmaker() as s:
        chat = (await s.scalars(select(Job).where(Job.kind == "chat"))).all()
    assert len(chat) == 1  # it went to the agent, not to the stopped task
    # the cabinet was configured (an adapter): it comes back as the same stop would leave it
    on = await campus.client.post(f"/api/v1/connections/{campus.cabinet}/enable")
    assert on.json()["status"] == "stale"


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


async def test_one_far_exam_does_not_make_every_day_up_to_it_missing_from_the_primary(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    """The cabinet's classes end on 31.10 (its sync window) and it has an exam on 20.12: a class of
    the calendar on 3.11 is not "missing" from a cabinet that does not reach that far."""
    await add(
        tx_sessionmaker,
        event(campus.user_id, campus.cabinet, at(9, day=27), title="Алгоритмы"),
        event(campus.user_id, campus.cabinet, at(9, day=30), title="Алгоритмы"),
        event(
            campus.user_id,
            campus.cabinet,
            datetime(2026, 12, 20, 9, 0, tzinfo=UTC),
            title="Экзамен: Алгоритмы",
            kind="exam",
        ),
        event(
            campus.user_id,
            campus.calendar,
            datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
            title="Алгоритмы",
        ),
    )
    [one] = await schedule(campus.client, start="2026-11-03T00:00:00Z", end="2026-11-04T00:00:00Z")
    assert titles(one["sources"]) == ["Календарь"]
    assert one["disagreement"] is None


async def test_a_class_within_the_reach_of_the_regular_classes_is_still_marked(
    campus: Campus, tx_sessionmaker: Sm
) -> None:
    await add(
        tx_sessionmaker,
        event(campus.user_id, campus.cabinet, at(9, day=23), title="Алгоритмы"),
        event(campus.user_id, campus.cabinet, at(9, day=30), title="Алгоритмы"),
        event(campus.user_id, campus.calendar, at(9, day=27), title="Алгоритмы"),
    )
    [one] = await schedule(campus.client, start="2026-10-27T00:00:00Z", end="2026-10-28T00:00:00Z")
    assert one["disagreement"]["kind"] == "not_in_primary"


async def test_the_read_of_a_schedule_has_the_indexes_it_needs(tx_sessionmaker: Sm) -> None:
    async with tx_sessionmaker() as s:
        names = set(
            await s.scalars(
                text("select indexname from pg_indexes where tablename = 'schedule_events'")
            )
        )
    assert "ix_schedule_events_conn_start" in names


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
