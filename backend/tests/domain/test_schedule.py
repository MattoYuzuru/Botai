"""Schedule reads and cross-space conflicts (FR-012, FR-013, NFR-001)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import update

from botai.core.clock import FrozenClock
from botai.db.models import Connection
from tests.auth.support import Sm, make_connection, make_space, signup
from tests.domain.helpers import World, add, at, course, event

WEEK = {"start": "2026-10-26T00:00:00Z", "end": "2026-11-02T00:00:00Z"}


async def _schedule(c: httpx.AsyncClient, **params: str) -> list[dict[str, Any]]:
    r = await c.get("/api/v1/schedule", params={**WEEK, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_events_of_all_spaces_in_time_order_with_provenance(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    await add(
        tx_sessionmaker,
        event(
            w.user_id,
            w.conn_b,
            at(14),
            title="Б: семинар",
            kind="seminar",
            format="online",
            location_text="Zoom",
            teacher_text="Петров",
            url="https://b.example.org/e/1",
        ),
        event(
            w.user_id,
            w.conn_a,
            at(9),
            title="А: лекция",
            kind="lecture",
            format="offline",
            location_text="Ауд. 101",
            changed=True,
        ),
    )
    events = await _schedule(w.client)
    assert [e["title"] for e in events] == ["А: лекция", "Б: семинар"]
    first, second = events
    assert first["kind"] == "lecture"
    assert first["location"] == "Ауд. 101"
    assert first["changed"] is True
    assert first["provenance"] == {
        "connectionId": w.conn_a,
        "spaceId": w.space_a,
        "sourceUrl": "https://source.example.org/x",
        "fetchedAt": "2026-10-27T12:00:00Z",
        "stale": False,
    }
    assert second["provenance"]["spaceId"] == w.space_b
    assert second["teacher"] == "Петров"
    assert second["format"] == "online"
    assert second["url"] == "https://b.example.org/e/1"


async def test_window_is_half_open(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    await add(
        tx_sessionmaker,
        event(w.user_id, w.conn_a, at(8), at(9), title="кончается ровно в начале окна"),
        event(w.user_id, w.conn_a, at(9), at(10), title="внутри"),
        event(w.user_id, w.conn_a, at(10), at(11), title="начинается ровно в конце окна"),
        event(w.user_id, w.conn_a, at(8, 30), at(9, 30), title="заходит слева"),
    )
    events = await _schedule(w.client, start="2026-10-27T09:00:00Z", end="2026-10-27T10:00:00Z")
    assert [e["title"] for e in events] == ["заходит слева", "внутри"]


async def test_space_filter_and_course_title(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    mat = course(w.user_id, w.conn_a, "Математический анализ", ext="c-1")
    await add(
        tx_sessionmaker,
        mat,
        event(w.user_id, w.conn_a, at(9), title="Лекция", course_external_id="c-1"),
        event(w.user_id, w.conn_b, at(11), title="Чужой вуз"),
    )
    only_a = await _schedule(w.client, space_id=w.space_a)
    assert [e["title"] for e in only_a] == ["Лекция"]
    assert only_a[0]["courseTitle"] == "Математический анализ"
    both = await _schedule(w.client)
    assert both[1]["courseTitle"] is None


async def test_overlap_between_spaces_is_a_conflict_for_both_sides(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    a = event(w.user_id, w.conn_a, at(10), at(11, 30), title="А")
    b = event(w.user_id, w.conn_b, at(11), at(12, 30), title="Б")
    free = event(w.user_id, w.conn_a, at(15), title="Без пересечений")
    await add(tx_sessionmaker, a, b, free)
    events = {e["title"]: e for e in await _schedule(w.client)}
    assert events["А"]["conflictsWith"] == [b.id]
    assert events["Б"]["conflictsWith"] == [a.id]
    assert events["Без пересечений"]["conflictsWith"] == []


async def test_touching_ends_and_same_space_overlaps_are_not_conflicts(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    await add(
        tx_sessionmaker,
        event(w.user_id, w.conn_a, at(9), at(10), title="кончается в 10"),
        event(w.user_id, w.conn_b, at(10), at(11), title="начинается в 10"),
        event(w.user_id, w.conn_a, at(13), at(15), title="свой раз"),
        event(w.user_id, w.conn_a, at(14), at(16), title="свой два"),
    )
    assert all(e["conflictsWith"] == [] for e in await _schedule(w.client))


async def test_a_partner_outside_the_window_still_counts(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    early = event(w.user_id, w.conn_b, at(8), at(9, 30), title="до окна")
    inside = event(w.user_id, w.conn_a, at(9), at(11), title="в окне")
    await add(tx_sessionmaker, early, inside)
    events = await _schedule(w.client, start="2026-10-27T10:00:00Z", end="2026-10-27T12:00:00Z")
    assert [e["title"] for e in events] == ["в окне"]
    assert events[0]["conflictsWith"] == [early.id]


async def test_space_filter_keeps_conflicts_with_the_other_space(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    mine = event(w.user_id, w.conn_a, at(10), at(11), title="А")
    theirs = event(w.user_id, w.conn_b, at(10, 30), at(11, 30), title="Б")
    await add(tx_sessionmaker, mine, theirs)
    events = await _schedule(w.client, space_id=w.space_a)
    assert [e["title"] for e in events] == ["А"]
    assert events[0]["conflictsWith"] == [theirs.id]


async def test_three_way_conflicts_list_every_other_space_event(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    sc = await make_space(tx_sessionmaker, w.user_id, "Вуз В")
    cc = await make_connection(tx_sessionmaker, w.user_id, sc)
    a = event(w.user_id, w.conn_a, at(10), at(12), title="А")
    b = event(w.user_id, w.conn_b, at(10), at(12), title="Б")
    c = event(w.user_id, cc, at(11), at(13), title="В")
    await add(tx_sessionmaker, a, b, c)
    got = {e["title"]: sorted(e["conflictsWith"]) for e in await _schedule(w.client)}
    assert got == {"А": sorted([b.id, c.id]), "Б": sorted([a.id, c.id]), "В": sorted([a.id, b.id])}


async def test_disabled_connections_leave_the_calendar(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    await add(
        tx_sessionmaker,
        event(w.user_id, w.conn_a, at(9), title="остаётся"),
        event(w.user_id, w.conn_b, at(9), title="отключено"),
    )
    async with tx_sessionmaker() as s:
        await s.execute(
            update(Connection).where(Connection.id == w.conn_b).values(status="disabled")
        )
        await s.commit()
    assert [e["title"] for e in await _schedule(w.client)] == ["остаётся"]


async def test_unexpected_stored_values_degrade_to_unknown_not_to_500(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    await add(tx_sessionmaker, event(w.user_id, w.conn_a, at(9), kind="webinar", format="vr"))
    [e] = await _schedule(w.client)
    assert (e["kind"], e["format"]) == ("other", "unknown")


async def test_range_validation(world: World) -> None:
    c = world.client
    inverted = await c.get("/api/v1/schedule", params={"start": WEEK["end"], "end": WEEK["start"]})
    assert inverted.status_code == 422
    too_long = await c.get(
        "/api/v1/schedule", params={"start": "2026-01-01T00:00:00Z", "end": "2028-01-01T00:00:00Z"}
    )
    assert too_long.status_code == 422
    missing = await c.get("/api/v1/schedule")
    assert missing.status_code == 422


async def test_naive_datetimes_are_read_as_utc(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    await add(tx_sessionmaker, event(w.user_id, w.conn_a, at(9), title="девять UTC"))
    got = await _schedule(w.client, start="2026-10-27T09:00:00", end="2026-10-27T10:00:00")
    assert [e["title"] for e in got] == ["девять UTC"]


async def test_unknown_space_filter_is_404_not_an_empty_list(world: World) -> None:
    r = await world.client.get(
        "/api/v1/schedule", params={**WEEK, "space_id": "01J0000000000000000000NONE"}
    )
    assert r.status_code == 404


async def test_nothing_of_another_user_is_visible(
    world: World,
    tx_sessionmaker: Sm,
    new_client: Callable[[], httpx.AsyncClient],
    clock: FrozenClock,
) -> None:
    w = world
    await add(tx_sessionmaker, event(w.user_id, w.conn_a, at(9), title="секрет Алисы"))
    bob_client = new_client()
    bob = await signup(bob_client, tx_sessionmaker, clock, email="bob@example.com")
    bob_space = await make_space(tx_sessionmaker, bob.id)
    bob_conn = await make_connection(tx_sessionmaker, bob.id, bob_space)
    await add(tx_sessionmaker, event(bob.id, bob_conn, at(9), title="Боб"))

    assert [e["title"] for e in await _schedule(bob_client)] == ["Боб"]
    foreign = await bob_client.get("/api/v1/schedule", params={**WEEK, "space_id": w.space_a})
    assert foreign.status_code == 404
    assert "секрет" not in foreign.text
    # and the other way round: Alice does not see Bob's
    assert "Боб" not in str(await _schedule(w.client))


async def test_conflicts_never_cross_users(
    world: World,
    tx_sessionmaker: Sm,
    new_client: Callable[[], httpx.AsyncClient],
    clock: FrozenClock,
) -> None:
    w = world
    mine = event(w.user_id, w.conn_a, at(9), at(10), title="моя")
    await add(tx_sessionmaker, mine)
    bob_client = new_client()
    bob = await signup(bob_client, tx_sessionmaker, clock, email="bob@example.com")
    bob_conn = await make_connection(
        tx_sessionmaker, bob.id, await make_space(tx_sessionmaker, bob.id)
    )
    await add(tx_sessionmaker, event(bob.id, bob_conn, at(9), at(10), title="Боба"))
    assert (await _schedule(bob_client))[0]["conflictsWith"] == []
    assert (await _schedule(w.client))[0]["conflictsWith"] == []


async def test_requires_a_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/schedule", params=WEEK)).status_code == 401
    assert (await client.get("/api/v1/today")).status_code == 401
