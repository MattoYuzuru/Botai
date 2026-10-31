"""The read path over both provenance paths: v4 `source_id` rows and the unchanged v3 answers.

The v4 playbook runner writes the same domain tables with `source_id` (03-sources-and-ux §6.1); the
API reads them by the space of their source (FR-054/FR-062, 16-sources-runner-tools §6.1). These
tests build the rows through the writer/ORM, not through a run: the schedule/tasks/etc. reads are
what is proven here, the end-to-end fakeuni sync is `tests/sources/v4/test_read_path.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from botai.core.ids import new_id
from botai.db.models import (
    Course,
    Deadline,
    Grade,
    NewsItem,
    Person,
    ScheduleEvent,
    SourcePriority,
)
from botai.sources.v4 import service as v4_service
from tests.auth.support import Sm
from tests.domain.helpers import T0, World, add, at, course, deadline, event, grade, news, person

pytestmark = pytest.mark.db


async def _v4_source(
    sm: Sm,
    user_id: str,
    space_id: str,
    *,
    title: str = "ЛК Fakeuni",
    url: str = "https://fakeuni.test/app",
    status: str = "active",
) -> str:
    """A v4 source of the given space; its rows are written by the v4 path (no connection)."""
    async with sm() as session:
        source = await v4_service.create_source(
            session,
            user_id=user_id,
            space_id=space_id,
            title=title,
            entry_url=url,
            kinds=["site", "schedule", "deadlines", "grades", "people", "news", "courses"],
            status=status,
        )
        await session.commit()
        return source.id


def v4_event(
    user_id: str,
    source_id: str,
    starts_at: datetime,
    ends_at: datetime | None = None,
    *,
    title: str = "Пара",
    ext: str | None = None,
    **kw: Any,
) -> ScheduleEvent:
    return ScheduleEvent(
        id=new_id(),
        user_id=user_id,
        connection_id=None,
        source_id=source_id,
        external_id=ext or new_id(),
        source_url="https://fakeuni.test/api/v2/timetable",
        open_url="https://fakeuni.test/app",
        fetched_at=T0,
        seen_at=T0,
        title=title,
        starts_at=starts_at,
        ends_at=ends_at or starts_at + timedelta(minutes=80),
        **kw,
    )


def _row(model: type[Any], user_id: str, source_id: str, ext: str | None = None) -> Any:
    return model(
        id=new_id(),
        user_id=user_id,
        connection_id=None,
        source_id=source_id,
        external_id=ext or new_id(),
        source_url="https://fakeuni.test/api",
        open_url="https://fakeuni.test/app",
        fetched_at=T0,
        seen_at=T0,
    )


def v4_deadline(
    user_id: str, source_id: str, due_at: datetime | None, *, title: str = "Сдать работу", **kw: Any
) -> Deadline:
    row = _row(Deadline, user_id, source_id, kw.pop("ext", None))
    row.title = title
    row.due_at = due_at
    row.state = "open"
    for name, value in kw.items():
        setattr(row, name, value)
    return row


def v4_course(user_id: str, source_id: str, title: str = "Матан", **kw: Any) -> Course:
    row = _row(Course, user_id, source_id, kw.pop("ext", None))
    row.title = title
    for name, value in kw.items():
        setattr(row, name, value)
    return row


def v4_grade(user_id: str, source_id: str, value: str = "8", **kw: Any) -> Grade:
    row = _row(Grade, user_id, source_id, kw.pop("ext", None))
    row.value = value
    for name, value_ in kw.items():
        setattr(row, name, value_)
    return row


def v4_person(user_id: str, source_id: str, name: str = "Иванов И. И.", **kw: Any) -> Person:
    row = _row(Person, user_id, source_id, kw.pop("ext", None))
    row.name = name
    for name_, value in kw.items():
        setattr(row, name_, value)
    return row


def v4_news(user_id: str, source_id: str, title: str = "Новость", **kw: Any) -> NewsItem:
    row = _row(NewsItem, user_id, source_id, kw.pop("ext", None))
    row.title = title
    for name, value in kw.items():
        setattr(row, name, value)
    return row


async def _get(client: httpx.AsyncClient, path: str, **params: str) -> Any:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _priorities(sm: Sm, space_id: str, kind: str, source_ids: list[str]) -> None:
    async with sm() as session:
        session.add(SourcePriority(space_id=space_id, kind=kind, source_ids=source_ids))
        await session.commit()


# --- the v4 rows are read by their space ---------------------------------------------------------


async def test_v4_rows_of_a_space_appear_in_every_read(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    await add(
        tx_sessionmaker,
        v4_event(w.user_id, source, at(10), title="Матанализ", kind="lecture"),
        v4_deadline(w.user_id, source, at(12, day=30), title="Домашка"),
        v4_course(w.user_id, source, "Математический анализ", code="MATH"),
        v4_grade(w.user_id, source, "9", title="Контрольная"),
        v4_person(w.user_id, source, "Петров П. П.", role="lecturer"),
        v4_news(w.user_id, source, "Объявление", published_at=at(9, day=26)),
    )

    schedule = await _get(
        w.client,
        "/api/v1/schedule",
        start=at(0).isoformat(),
        end=at(23).isoformat(),
    )
    assert [(e["title"], e["provenance"]["sourceId"]) for e in schedule] == [("Матанализ", source)]
    assert schedule[0]["provenance"]["connectionId"] is None
    assert schedule[0]["provenance"]["spaceId"] == w.space_a
    assert schedule[0]["sources"] == [
        {"connectionId": None, "sourceId": source, "title": "ЛК Fakeuni"}
    ]

    deadlines = await _get(w.client, "/api/v1/deadlines")
    assert [d["title"] for d in deadlines] == ["Домашка"]
    assert deadlines[0]["provenance"] == {
        "connectionId": None,
        "sourceId": source,
        "spaceId": w.space_a,
        "sourceUrl": "https://fakeuni.test/api",
        "openUrl": "https://fakeuni.test/app",
        "fetchedAt": "2026-10-27T12:00:00Z",
        "stale": False,
    }

    courses = await _get(w.client, "/api/v1/courses")
    assert [(c["title"], c["code"], c["provenance"]["sourceId"]) for c in courses] == [
        ("Математический анализ", "MATH", source)
    ]
    grades = await _get(w.client, "/api/v1/grades")
    assert [(g["value"], g["provenance"]["sourceId"]) for g in grades] == [("9", source)]
    people = await _get(w.client, "/api/v1/people")
    assert [(p["name"], p["provenance"]["sourceId"]) for p in people] == [("Петров П. П.", source)]
    news_rows = await _get(w.client, "/api/v1/news")
    assert [(n["title"], n["provenance"]["sourceId"]) for n in news_rows] == [
        ("Объявление", source)
    ]

    today = await _get(w.client, "/api/v1/today")
    assert [(e["title"], e["provenance"]["sourceId"]) for e in today["events"]] == [
        ("Матанализ", source)
    ]
    assert [d["title"] for d in today["deadlines"]] == ["Домашка"]

    sections = await _get(w.client, f"/api/v1/spaces/{w.space_a}/sections")
    assert {s["kind"]: s["count"] for s in sections} == {
        "courses": 1,
        "scheduleEvents": 1,
        "deadlines": 1,
        "grades": 1,
        "people": 1,
        "news": 1,
    }


async def test_v4_rows_are_not_mixed_between_spaces(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    await add(tx_sessionmaker, v4_deadline(w.user_id, source, at(12, day=30), title="Только А"))
    assert [d["title"] for d in await _get(w.client, "/api/v1/deadlines", space_id=w.space_a)] == [
        "Только А"
    ]
    assert await _get(w.client, "/api/v1/deadlines", space_id=w.space_b) == []
    # a foreign (unknown) space is a 404, not an empty list
    response = await w.client.get("/api/v1/deadlines", params={"space_id": "01JUNKNOWN"})
    assert response.status_code == 404


# --- switched off and stale ---------------------------------------------------------------------


async def test_a_disabled_source_is_hidden_everywhere(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    await add(
        tx_sessionmaker,
        v4_event(w.user_id, source, at(10), title="Скрытая пара"),
        v4_deadline(w.user_id, source, at(12, day=30), title="Скрытый дедлайн"),
        v4_course(w.user_id, source, "Скрытый курс"),
    )
    async with tx_sessionmaker() as session:
        await v4_service.disable_source(session, user_id=w.user_id, source_id=source)
        await session.commit()

    assert await _get(w.client, "/api/v1/deadlines") == []
    assert await _get(w.client, "/api/v1/courses") == []
    assert (
        await _get(w.client, "/api/v1/schedule", start=at(0).isoformat(), end=at(23).isoformat())
        == []
    )
    assert await _get(w.client, f"/api/v1/spaces/{w.space_a}/sections") == []
    # nothing was deleted: the rows come back with the source
    async with tx_sessionmaker() as session:
        await v4_service.enable_source(session, user_id=w.user_id, source_id=source)
        await session.commit()
    assert [d["title"] for d in await _get(w.client, "/api/v1/deadlines")] == ["Скрытый дедлайн"]


async def test_a_stale_source_keeps_its_rows_but_marks_them_stale(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    await add(tx_sessionmaker, v4_deadline(w.user_id, source, at(12, day=30), title="Поздний"))
    async with tx_sessionmaker() as session:
        await v4_service.set_status(
            session, user_id=w.user_id, source_id=source, status="needs_reauth"
        )
        await session.commit()
    deadlines = await _get(w.client, "/api/v1/deadlines")
    assert [d["title"] for d in deadlines] == ["Поздний"]
    assert deadlines[0]["provenance"]["stale"] is True


# --- priority: the primary source decides -------------------------------------------------------


async def test_schedule_priority_from_source_priorities_merges_v3_and_v4(
    world: World, tx_sessionmaker: Sm
) -> None:
    """A v3 cabinet and a v4 calendar describe the same class; the v4 priority list makes the v4
    source the primary, so its title/room decide and both sources are shown (FR-054/FR-062)."""
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a, title="Календарь")
    await add(
        tx_sessionmaker,
        event(
            w.user_id,
            w.conn_a,
            at(10),
            at(11, 20),
            title="Математический анализ",
            location_text="Ауд. 101",
        ),
        v4_event(
            w.user_id,
            source,
            at(10),
            at(11, 30),
            title="Математический анализ",
            location_text="Ауд. 305",
        ),
    )
    before = await _get(
        w.client, "/api/v1/schedule", start=at(0).isoformat(), end=at(23).isoformat()
    )
    # Without a setting the v3 connection is the default primary (oldest healthy source), and the
    # two rows are already one merged class of both sources.
    assert len(before) == 1
    assert before[0]["provenance"]["connectionId"] == w.conn_a
    assert before[0]["location"] == "Ауд. 101"
    assert [s["sourceId"] for s in before[0]["sources"]] == [None, source]

    await _priorities(tx_sessionmaker, w.space_a, "schedule", [source])
    after = await _get(
        w.client, "/api/v1/schedule", start=at(0).isoformat(), end=at(23).isoformat()
    )
    assert len(after) == 1
    merged = after[0]
    assert merged["title"] == "Математический анализ"  # the primary's title
    assert merged["location"] == "Ауд. 305"
    assert merged["disagreement"]["kind"] == "location"
    assert merged["provenance"]["sourceId"] == source
    assert merged["provenance"]["connectionId"] is None
    assert [s["sourceId"] for s in merged["sources"]] == [source, None]
    assert [s["connectionId"] for s in merged["sources"]] == [None, w.conn_a]


async def test_non_schedule_dedupe_prefers_the_priority_source(
    world: World, tx_sessionmaker: Sm
) -> None:
    """The same deadline of a v3 connection and a v4 source is one row; the priority list decides
    which one (the v3 connection is older and healthy, so it would win by default)."""
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    await add(
        tx_sessionmaker,
        deadline(w.user_id, w.conn_a, at(12, day=30), title="Сдать работу"),
        v4_deadline(w.user_id, source, at(12, day=30), title="Сдать работу"),
    )
    default = await _get(w.client, "/api/v1/deadlines")
    assert [d["provenance"]["sourceId"] for d in default] == [None]

    await _priorities(tx_sessionmaker, w.space_a, "deadlines", [source])
    preferred = await _get(w.client, "/api/v1/deadlines")
    assert len(preferred) == 1
    assert preferred[0]["provenance"]["sourceId"] == source


async def test_v3_only_answers_stay_exactly_as_before(world: World, tx_sessionmaker: Sm) -> None:
    """A space without v4 sources: same rows, same order, same fields — the only difference of the
    new contract is the explicit `sourceId: null` next to the connection provenance."""
    w = world
    await add(
        tx_sessionmaker,
        deadline(w.user_id, w.conn_b, at(12, day=29), title="ранний"),
        deadline(w.user_id, w.conn_b, at(12, day=31), title="поздний"),
        # the same natural key twice from two v3 connections is shown twice, as before
        deadline(w.user_id, w.conn_a, at(12, day=30), title="одинаковый"),
        deadline(w.user_id, w.conn_b, at(12, day=30), title="одинаковый"),
        event(w.user_id, w.conn_a, at(10), title="А: лекция", kind="lecture"),
        course(w.user_id, w.conn_a, "Матан"),
        grade(w.user_id, w.conn_a, "8"),
        person(w.user_id, w.conn_a),
        news(w.user_id, w.conn_a, at(9), "Новость"),
    )
    deadlines = await _get(w.client, "/api/v1/deadlines")
    assert [d["title"] for d in deadlines] == ["ранний", "одинаковый", "одинаковый", "поздний"]
    assert {d["provenance"]["connectionId"] for d in deadlines if d["title"] == "одинаковый"} == {
        w.conn_a,
        w.conn_b,
    }
    assert all(d["provenance"]["sourceId"] is None for d in deadlines)
    schedule = await _get(
        w.client, "/api/v1/schedule", start=at(0).isoformat(), end=at(23).isoformat()
    )
    assert [(e["title"], e["provenance"]["connectionId"]) for e in schedule] == [
        ("А: лекция", w.conn_a)
    ]
    assert schedule[0]["sources"] == [
        {"connectionId": w.conn_a, "sourceId": None, "title": "Источник"}
    ]
    assert [
        (c["title"], c["provenance"]["connectionId"])
        for c in await _get(w.client, "/api/v1/courses")
    ] == [("Матан", w.conn_a)]
    assert [
        (p["name"], p["provenance"]["connectionId"]) for p in await _get(w.client, "/api/v1/people")
    ] == [("Иванов И. И.", w.conn_a)]
    assert [
        (g["value"], g["provenance"]["connectionId"])
        for g in await _get(w.client, "/api/v1/grades")
    ] == [("8", w.conn_a)]
    assert [
        (n["title"], n["provenance"]["connectionId"]) for n in await _get(w.client, "/api/v1/news")
    ] == [("Новость", w.conn_a)]


async def test_a_second_v4_source_joins_the_merge_as_additional(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    first = await _v4_source(
        tx_sessionmaker, w.user_id, w.space_a, title="Кабинет", url="https://a.test/app"
    )
    second = await _v4_source(
        tx_sessionmaker, w.user_id, w.space_a, title="Календарь", url="https://a.test/calendar"
    )
    await add(
        tx_sessionmaker,
        v4_event(w.user_id, first, at(10), title="Матанализ", location_text="305"),
        v4_event(
            w.user_id, second, at(10, 5), at(11, 25), title="Матанализ", teacher_text="Иванов"
        ),
    )
    await _priorities(tx_sessionmaker, w.space_a, "schedule", [second, first])
    merged = await _get(
        w.client, "/api/v1/schedule", start=at(0).isoformat(), end=at(23).isoformat()
    )
    assert len(merged) == 1
    assert merged[0]["location"] == "305"  # not in the primary, filled from the additional
    assert merged[0]["teacher"] == "Иванов"
    assert [s["sourceId"] for s in merged[0]["sources"]] == [second, first]
    assert merged[0]["disagreement"]["kind"] == "time"


# --- the loader itself --------------------------------------------------------------------------


async def test_loader_reads_both_paths_in_two_queries(tx_sessionmaker: Sm, world: World) -> None:
    from botai.domain import sources as domain_sources

    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    async with tx_sessionmaker() as session:
        live = await domain_sources.load(session, w.user_id)
    assert set(live.connections) == {w.conn_a, w.conn_b}
    assert set(live.sources) == {source}
    assert live.get(w.conn_a) is not None
    assert live.get(w.conn_b) is not None
    assert live.get(source) is not None
    assert live.get("01JNOSUCHSOURCE") is None
    assert live.attention() == []
    order = live.default_order()[w.space_a]
    assert [s.id for s in order] == [w.conn_a, source]

    async with tx_sessionmaker() as session:
        await v4_service.disable_source(session, user_id=w.user_id, source_id=source)
        await session.commit()
    async with tx_sessionmaker() as session:
        hidden = await domain_sources.load(session, w.user_id)
    assert set(hidden.sources) == set()


async def test_loader_dedupe_does_not_touch_a_v3_only_duplicate(
    world: World, tx_sessionmaker: Sm
) -> None:
    from botai.domain import sources as domain_sources

    w = world
    await add(
        tx_sessionmaker,
        deadline(w.user_id, w.conn_a, at(12, day=30), title="одинаковый"),
        deadline(w.user_id, w.conn_b, at(12, day=30), title="одинаковый"),
    )
    async with tx_sessionmaker() as session:
        live = await domain_sources.load(session, w.user_id)
        preferences = await domain_sources.preferences(session, w.user_id, live, ["deadlines"])
        rows = list(await session.scalars(select(Deadline).where(Deadline.user_id == w.user_id)))
        kept = domain_sources.dedupe_preferring(
            rows,
            live=live,
            preferences=preferences,
            kind="deadlines",
            key_of=lambda row: (live.resolve(row).space_id, row.title, row.due_at),
        )
    assert len(kept) == 2
