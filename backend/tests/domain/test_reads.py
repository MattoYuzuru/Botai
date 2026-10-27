"""Deadlines, courses, grades, people, news, staleness and today (FR-012, FR-022, NFR-001/007/009)."""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from botai.core.clock import FrozenClock
from botai.core.ids import new_id
from botai.db.models import AdapterSpecRow, Connection, Space
from tests.auth.support import Sm, make_connection, make_space, signup
from tests.domain.helpers import (
    T0,
    World,
    add,
    at,
    course,
    deadline,
    event,
    grade,
    news,
    person,
)


async def _get(c: httpx.AsyncClient, path: str, **params: str) -> Any:
    r = await c.get(path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --- deadlines ----------------------------------------------------------------------------------


async def test_deadlines_sorted_by_effective_due_date_undated_last(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    await add(
        tx_sessionmaker,
        deadline(w.user_id, w.conn_a, at(12, day=30), title="поздний"),
        deadline(w.user_id, w.conn_a, None, title="без срока"),
        # extended: the original date says early, the effective one says late
        deadline(
            w.user_id, w.conn_b, at(12, day=28), effective_due_at=at(12, day=31), title="продлён"
        ),
        deadline(w.user_id, w.conn_b, at(12, day=29), title="ранний"),
    )
    got = await _get(w.client, "/api/v1/deadlines")
    assert [d["title"] for d in got] == ["ранний", "поздний", "продлён", "без срока"]
    extended = got[2]
    assert extended["dueAt"] == "2026-10-28T12:00:00Z"
    assert extended["effectiveDueAt"] == "2026-10-31T12:00:00Z"


async def test_done_work_is_hidden_unless_asked_for(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    await add(
        tx_sessionmaker,
        deadline(w.user_id, w.conn_a, at(12, day=29), state="open", title="открыт"),
        deadline(w.user_id, w.conn_a, at(12, day=29), state="missed", title="просрочен"),
        deadline(w.user_id, w.conn_a, at(12, day=29), state="unknown", title="неизвестно"),
        deadline(w.user_id, w.conn_a, at(12, day=29), state="submitted", title="сдан"),
        deadline(w.user_id, w.conn_a, at(12, day=29), state="graded", title="оценён"),
    )
    default = await _get(w.client, "/api/v1/deadlines")
    assert sorted(d["title"] for d in default) == ["неизвестно", "открыт", "просрочен"]
    everything = await _get(w.client, "/api/v1/deadlines", include_done="true")
    assert len(everything) == 5


async def test_deadlines_filter_by_space_and_carry_course(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    await add(
        tx_sessionmaker,
        course(w.user_id, w.conn_a, "Алгебра", ext="alg"),
        deadline(w.user_id, w.conn_a, at(12, day=29), title="А", course_external_id="alg"),
        deadline(w.user_id, w.conn_b, at(12, day=29), title="Б"),
    )
    got = await _get(w.client, "/api/v1/deadlines", space_id=w.space_a)
    assert [(d["title"], d["courseTitle"]) for d in got] == [("А", "Алгебра")]


# --- courses, grades, people, news ----------------------------------------------------------------


async def test_courses_skip_archived_and_sort_by_title(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    await add(
        tx_sessionmaker,
        course(
            w.user_id,
            w.conn_a,
            "Физика",
            code="PH",
            term="2026-осень",
            url="https://x.example.org/c",
        ),
        course(w.user_id, w.conn_b, "Алгебра"),
        course(w.user_id, w.conn_a, "Исчезнувший", archived=True),
    )
    got = await _get(w.client, "/api/v1/courses")
    assert [c["title"] for c in got] == ["Алгебра", "Физика"]
    assert got[1]["code"] == "PH"
    assert got[1]["term"] == "2026-осень"
    assert [c["title"] for c in await _get(w.client, "/api/v1/courses", space_id=w.space_b)] == [
        "Алгебра"
    ]


async def test_grades_newest_first_with_course_title(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    await add(
        tx_sessionmaker,
        course(w.user_id, w.conn_a, "Алгебра", ext="alg"),
        grade(
            w.user_id,
            w.conn_a,
            "7",
            title="старая",
            graded_at=at(9, day=20),
            course_external_id="alg",
        ),
        grade(
            w.user_id, w.conn_a, "9", title="новая", graded_at=at(9, day=25), scale="10", weight=0.3
        ),
        grade(w.user_id, w.conn_b, "Зачёт", title="без даты"),
    )
    got = await _get(w.client, "/api/v1/grades")
    assert [g["title"] for g in got] == ["новая", "старая", "без даты"]
    assert got[0]["scale"] == "10"
    assert got[0]["weight"] == 0.3
    assert got[1]["courseTitle"] == "Алгебра"


async def test_people_and_news(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    await add(
        tx_sessionmaker,
        person(w.user_id, w.conn_a, "Яковлев", role="teacher", email="y@example.org"),
        person(w.user_id, w.conn_b, "Алексеев", role="assistant"),
        news(w.user_id, w.conn_a, at(9, day=20), "старая"),
        news(w.user_id, w.conn_b, at(9, day=26), "свежая"),
    )
    people = await _get(w.client, "/api/v1/people")
    assert [p["name"] for p in people] == ["Алексеев", "Яковлев"]
    assert people[1]["email"] == "y@example.org"
    assert people[1]["role"] == "teacher"
    assert [n["title"] for n in await _get(w.client, "/api/v1/news")] == ["свежая", "старая"]
    assert [n["title"] for n in await _get(w.client, "/api/v1/news", space_id=w.space_a)] == [
        "старая"
    ]


async def test_empty_domain_is_an_empty_list_everywhere(world: World) -> None:
    for path in ("deadlines", "courses", "grades", "people", "news"):
        assert await _get(world.client, f"/api/v1/{path}") == []


# --- provenance.stale ---------------------------------------------------------------------------


async def _set_connection(sm: Sm, conn_id: str, **values: Any) -> None:
    async with sm() as s:
        await s.execute(update(Connection).where(Connection.id == conn_id).values(**values))
        await s.commit()


@pytest.mark.parametrize(
    ("status", "stale"),
    [
        ("active", False),
        ("syncing", False),
        ("stale", True),
        ("needs_reauth", True),
        ("repairing", True),
    ],
)
async def test_stale_follows_the_connection_status(
    world: World, tx_sessionmaker: Sm, status: str, stale: bool
) -> None:
    w = world
    await _set_connection(tx_sessionmaker, w.conn_a, status=status, last_sync_at=T0)
    await add(tx_sessionmaker, deadline(w.user_id, w.conn_a, at(12, day=29)))
    [d] = await _get(w.client, "/api/v1/deadlines")
    assert d["provenance"]["stale"] is stale


async def test_stale_follows_the_freshness_of_the_spec(
    world: World, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    w = world
    spec = AdapterSpecRow(
        id=new_id(),
        origin="https://a.example.org",
        adapter="recipe",
        revision=1,
        state="active",
        spec={
            "adapter": "recipe",
            "recipe": {"freshness": {"syncEveryMinutes": 30, "staleAfterMinutes": 60}},
        },
    )
    await add(tx_sessionmaker, spec, deadline(w.user_id, w.conn_a, at(12, day=29)))
    await _set_connection(
        tx_sessionmaker, w.conn_a, spec_id=spec.id, last_sync_at=T0 - timedelta(minutes=50)
    )
    [fresh] = await _get(w.client, "/api/v1/deadlines")
    assert fresh["provenance"]["stale"] is False

    clock.advance(timedelta(minutes=20))  # the last sync is now 70 minutes old, the spec allows 60
    [old] = await _get(w.client, "/api/v1/deadlines")
    assert old["provenance"]["stale"] is True


async def test_without_a_spec_the_default_freshness_applies(
    world: World, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    w = world
    await add(tx_sessionmaker, deadline(w.user_id, w.conn_a, at(12, day=29)))
    await _set_connection(tx_sessionmaker, w.conn_a, last_sync_at=T0 - timedelta(hours=11))
    assert (await _get(w.client, "/api/v1/deadlines"))[0]["provenance"]["stale"] is False
    clock.advance(timedelta(hours=2))
    assert (await _get(w.client, "/api/v1/deadlines"))[0]["provenance"]["stale"] is True


async def test_a_never_synced_connection_is_not_called_stale_by_time(
    world: World, tx_sessionmaker: Sm
) -> None:
    await add(tx_sessionmaker, deadline(world.user_id, world.conn_a, at(12, day=29)))
    assert (await _get(world.client, "/api/v1/deadlines"))[0]["provenance"]["stale"] is False


# --- today --------------------------------------------------------------------------------------


async def test_today_collects_classes_deadlines_and_attention(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    await _set_connection(tx_sessionmaker, w.conn_a, last_sync_at=T0)
    await _set_connection(tx_sessionmaker, w.conn_b, status="needs_reauth", last_sync_at=T0)
    await add(
        tx_sessionmaker,
        event(w.user_id, w.conn_a, at(7), title="утренняя"),  # 10:00 Moscow
        event(w.user_id, w.conn_b, at(13), title="дневная"),
        event(w.user_id, w.conn_a, at(7, day=28), title="завтра"),
        event(w.user_id, w.conn_a, at(7, day=26), title="вчера"),
        deadline(w.user_id, w.conn_a, at(12, day=29), title="ближний"),
        deadline(w.user_id, w.conn_a, at(12, day=28), title="ещё ближе"),
        deadline(w.user_id, w.conn_a, at(12, day=28), state="submitted", title="сдан"),
        deadline(w.user_id, w.conn_a, at(12, day=20), title="давний"),
        deadline(w.user_id, w.conn_a, None, title="без срока"),
    )
    body = await _get(w.client, "/api/v1/today")
    assert body["date"] == "2026-10-27"
    assert [e["title"] for e in body["events"]] == ["утренняя", "дневная"]
    assert [d["title"] for d in body["deadlines"]] == ["ещё ближе", "ближний"]
    assert body["attention"] == [w.conn_b]


async def test_today_uses_each_spaces_own_calendar_day(world: World, tx_sessionmaker: Sm) -> None:
    """Now is 12:00 UTC: 15:00 on the 27th in Moscow, 22:00 on the 27th in Vladivostok. Their
    calendar days start 17 hours apart, so the same instant is yesterday in one and today in the other."""
    w = world
    async with tx_sessionmaker() as s:
        await s.execute(
            update(Space).where(Space.id == w.space_b).values(time_zone="Asia/Vladivostok")
        )
        await s.commit()
    await add(
        tx_sessionmaker,
        # 15:00 UTC on the 26th = 18:00 on the 26th in Moscow, 01:00 on the 27th in Vladivostok
        event(w.user_id, w.conn_a, at(15, day=26), title="Москва: вчера"),
        event(w.user_id, w.conn_b, at(15, day=26), title="Владивосток: сегодня"),
        # 15:00 UTC on the 27th = 18:00 on the 27th in Moscow, 01:00 on the 28th in Vladivostok
        event(w.user_id, w.conn_a, at(15), title="Москва: сегодня"),
        event(w.user_id, w.conn_b, at(15), title="Владивосток: завтра"),
    )
    titles = [e["title"] for e in (await _get(w.client, "/api/v1/today"))["events"]]
    assert titles == ["Владивосток: сегодня", "Москва: сегодня"]


async def test_today_date_is_the_first_spaces_local_day(
    world: World, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    clock.advance(timedelta(hours=10))  # 22:00 UTC = 01:00 on the 28th in Moscow
    assert (await _get(world.client, "/api/v1/today"))["date"] == "2026-10-28"


async def test_today_for_a_user_without_spaces(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    assert await _get(client, "/api/v1/today") == {
        "date": "2026-10-27",
        "events": [],
        "deadlines": [],
        "attention": [],
    }


async def test_today_flags_conflicts_across_spaces(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    a = event(w.user_id, w.conn_a, at(9), at(10, 30), title="А")
    b = event(w.user_id, w.conn_b, at(10), at(11), title="Б")
    await add(tx_sessionmaker, a, b)
    events = {
        e["title"]: e["conflictsWith"] for e in (await _get(w.client, "/api/v1/today"))["events"]
    }
    assert events == {"А": [b.id], "Б": [a.id]}


async def test_disabled_connection_is_not_attention_and_not_data(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    await _set_connection(tx_sessionmaker, w.conn_b, status="disabled")
    await add(tx_sessionmaker, event(w.user_id, w.conn_b, at(13), title="отключено"))
    body = await _get(w.client, "/api/v1/today")
    assert body["events"] == []
    assert w.conn_b not in body["attention"]


# --- tenant isolation across the whole domain -----------------------------------------------------


async def test_user_b_sees_none_of_user_a_data(
    world: World,
    tx_sessionmaker: Sm,
    new_client: Callable[[], httpx.AsyncClient],
    clock: FrozenClock,
) -> None:
    w = world
    await add(
        tx_sessionmaker,
        event(w.user_id, w.conn_a, at(9), title="a-event"),
        deadline(w.user_id, w.conn_a, at(12, day=29), title="a-deadline"),
        course(w.user_id, w.conn_a, "a-course"),
        grade(w.user_id, w.conn_a, "10", title="a-grade"),
        person(w.user_id, w.conn_a, "a-person"),
        news(w.user_id, w.conn_a, at(9), "a-news"),
    )
    bob = new_client()
    await signup(bob, tx_sessionmaker, clock, email="bob@example.com")
    paths = {
        "/api/v1/deadlines": {},
        "/api/v1/courses": {},
        "/api/v1/grades": {},
        "/api/v1/people": {},
        "/api/v1/news": {},
        "/api/v1/schedule": {"start": "2026-10-26T00:00:00Z", "end": "2026-11-02T00:00:00Z"},
    }
    for path, params in paths.items():
        assert await _get(bob, path, **params) == [], path
        foreign = await bob.get(path, params={**params, "space_id": w.space_a})
        assert foreign.status_code == 404, path
        assert "a-" not in foreign.text
    today = await _get(bob, "/api/v1/today")
    assert (today["events"], today["deadlines"], today["attention"]) == ([], [], [])


async def test_every_read_needs_a_session(client: httpx.AsyncClient) -> None:
    for path in ("deadlines", "courses", "grades", "people", "news", "today"):
        assert (await client.get(f"/api/v1/{path}")).status_code == 401, path


# --- NFR-009 ------------------------------------------------------------------------------------


async def test_reading_a_stored_week_is_fast(world: World, tx_sessionmaker: Sm) -> None:
    """p95 <= 1.5 s for reading a stored schedule from a database of realistic size."""
    w = world
    rows = []
    for day in range(20, 32):
        for slot in range(8):
            for conn in (w.conn_a, w.conn_b):
                for n in range(8):
                    rows.append(
                        event(
                            w.user_id, conn, at(6 + slot, day=day), title=f"Пара {day}-{slot}-{n}"
                        )
                    )
    await add(tx_sessionmaker, *rows)  # ~1500 events
    other = await make_space(tx_sessionmaker, w.user_id, "Ещё")
    await make_connection(tx_sessionmaker, w.user_id, other)

    params = {"start": "2026-10-26T00:00:00Z", "end": "2026-11-02T00:00:00Z"}
    samples = []
    for _ in range(20):
        t = time.perf_counter()
        r = await w.client.get("/api/v1/schedule", params=params)
        samples.append(time.perf_counter() - t)
        assert r.status_code == 200
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    assert p95 <= 1.5, f"p95={p95:.3f}s median={statistics.median(samples):.3f}s"


async def test_deadline_carries_the_assignment_text(world: World, tx_sessionmaker: Sm) -> None:
    from botai.db.models import Assignment
    from tests.domain.helpers import _common  # pyright: ignore[reportPrivateUsage]

    w = world
    await add(
        tx_sessionmaker,
        Assignment(
            title="ДЗ 3",
            description="Реализовать дерево отрезков",
            **_common(w.user_id, w.conn_a, "as-3"),
        ),
        deadline(w.user_id, w.conn_a, at(12, day=29), title="ДЗ 3", assignment_external_id="as-3"),
        deadline(w.user_id, w.conn_a, at(13, day=29), title="Без текста"),
    )
    got = await _get(w.client, "/api/v1/deadlines", space_id=w.space_a)
    assert [(d["title"], d["description"]) for d in got] == [
        ("ДЗ 3", "Реализовать дерево отрезков"),
        ("Без текста", None),
    ]
