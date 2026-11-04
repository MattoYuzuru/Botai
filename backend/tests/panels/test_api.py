"""`/api/v1/panels`: owner-only, default panels made on first read, resolved bounded data (D-039)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from botai.db.models import Panel
from botai.panels import service
from tests.auth.support import FrozenClock, Sm, make_space, signup
from tests.panels.support import (
    G,
    add_course,
    add_grades,
    add_profile,
    add_ranking,
    at,
    make_source,
)

pytestmark = pytest.mark.db

PANEL_KEYS = {
    "id",
    "key",
    "type",
    "spec",
    "status",
    "visible",
    "hiddenInAll",
    "position",
    "origin",
    "sourceId",
    "createdAt",
    "updatedAt",
    "space",
    "data",
}


async def _student(
    client: httpx.AsyncClient, sm: Sm, clock: FrozenClock, title: str = "Университет"
) -> tuple[str, str, str]:
    """A signed-up student with one university and one source: `(user_id, space_id, source_id)`."""
    account = await signup(client, sm, clock)
    space_id = await make_space(sm, account.id, title)
    source_id = await make_source(sm, user_id=account.id, space_id=space_id)
    return account.id, space_id, source_id


async def _panels(client: httpx.AsyncClient, **params: str) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/panels", params=params)
    assert response.status_code == 200, response.text
    return list(response.json())


def _by_key(panels: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {p["key"]: p for p in panels}


async def _full_student(
    client: httpx.AsyncClient, sm: Sm, clock: FrozenClock
) -> tuple[str, str, str]:
    user_id, space_id, source_id = await _student(client, sm, clock)
    math = await add_course(
        sm, user_id=user_id, source_id=source_id, external_id="mat", title="Математический анализ"
    )
    assert math
    await add_grades(
        sm,
        user_id=user_id,
        source_id=source_id,
        grades=[
            G("Коллоквиум", "8", "10 баллов", 8.0, 10.0, at(12), "mat"),
            G("Контрольная", "8,5", "из 10", 8.5, 10.0, at(19), "mat"),
            G("Лабораторная", "92", "100", 92.0, 100.0, at(22)),
            G("Зачёт", "зачтено", "зачёт/незачёт", graded_at=at(23)),
        ],
    )
    await add_profile(
        sm,
        user_id=user_id,
        source_id=source_id,
        fields=[
            ("year", "Курс", "3", "Обучение"),
            ("group", "Группа", "ПМ-31", "Обучение"),
            ("programme", "Программа", "Прикладная математика", None),
        ],
    )
    await add_ranking(
        sm,
        user_id=user_id,
        source_id=source_id,
        board="Рейтинг потока",
        places=[
            (1, "Участник 17", "96,4", False),
            (2, "Участник 08", "94,0", False),
            (3, "Участник 31", "92,5", False),
            (14, "Иванова А.", "81,2", True),
        ],
    )
    return user_id, space_id, source_id


# --- access -------------------------------------------------------------------------------------


async def test_the_panels_api_is_off_on_the_v3_engine(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    response = await client.get("/api/v1/panels")
    assert response.status_code == 404
    assert "BOTAI_AGENT_ENGINE=v4" in response.json()["detail"]


async def test_the_panels_api_needs_a_session(v4_client: httpx.AsyncClient) -> None:
    for call in (
        v4_client.get("/api/v1/panels"),
        v4_client.patch("/api/v1/panels/x", json={"visible": False}),
        v4_client.post("/api/v1/panels/x/accept"),
        v4_client.post("/api/v1/panels/x/dismiss"),
    ):
        assert (await call).status_code == 401


# --- default panels and their data ---------------------------------------------------------------


async def test_first_read_makes_the_default_panels_with_data(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    _, space_id, _ = await _full_student(v4_client, tx_sessionmaker, clock)
    panels = await _panels(v4_client)
    assert [p["key"] for p in panels][:3] == ["grades-overview", "grades-chart", "student-profile"]
    assert len(panels) == 4
    for panel in panels:
        assert set(panel) == PANEL_KEYS  # camelCase, nothing else
        assert panel["status"] == "active"
        assert panel["origin"] == "system"
        assert panel["visible"] is True
        assert panel["hiddenInAll"] is False
        assert panel["sourceId"] is None
        assert panel["space"] == {
            "id": space_id,
            "title": "Университет",
            "shortTitle": "Университет",
            "accentHue": 262.2,
        }
        assert panel["spec"]["type"] == panel["type"]
        assert panel["spec"]["v"] == 1
    assert [p["position"] for p in panels] == [0, 1, 2, 3]

    kpi, chart, profile, ranking = (panels[0], panels[1], panels[2], panels[3])
    assert kpi["spec"] == {
        "v": 1,
        "type": "kpi",
        "title": "Успеваемость",
        "icon": "grade",
        "dataset": {"kind": "grades", "board": None, "courseId": None},  # unused selectors: null
        "view": {"metrics": ["count", "latest"]},
    }
    assert ranking["key"].startswith("ranking:")
    assert ranking["spec"]["dataset"] == {
        "kind": "ranking",
        "board": "Рейтинг потока",
        "courseId": None,
    }

    # grades: every one counted, the newest first; numbers only where the source gave them
    assert kpi["data"]["kind"] == "grades"
    assert kpi["data"]["total"] == 4
    assert kpi["data"]["numeric"] == 3
    assert kpi["data"]["points"] == []  # tiles need no list
    assert kpi["data"]["latest"]["title"] == "Зачёт"
    assert kpi["data"]["latest"]["valueNum"] is None  # "зачтено" is not a number
    # the best grade is picked inside the most used scale (10), never across scales
    assert kpi["data"]["best"]["title"] == "Контрольная"
    assert kpi["data"]["best"]["valueNum"] == 8.5
    assert kpi["data"]["best"]["scaleMax"] == 10
    assert set(kpi["data"]["latest"]) == {
        "id",
        "title",
        "course",
        "courseId",
        "value",
        "scale",
        "valueNum",
        "scaleMax",
        "gradedAt",
    }

    # the chart: numeric grades, oldest first, each in its own scale
    points = chart["data"]["points"]
    assert [p["title"] for p in points] == ["Коллоквиум", "Контрольная", "Лабораторная"]
    assert [(p["valueNum"], p["scaleMax"]) for p in points] == [(8, 10), (8.5, 10), (92, 100)]
    assert points[0]["course"] == "Математический анализ"
    assert points[0]["value"] == "8"  # the text of the source stays beside the number
    assert points[2]["course"] is None

    assert profile["data"] == {
        "kind": "profile",
        "total": 3,
        "fields": [
            {
                "field": "programme",
                "label": "Программа",
                "value": "Прикладная математика",
                "group": None,
            },
            {"field": "group", "label": "Группа", "value": "ПМ-31", "group": "Обучение"},
            {"field": "year", "label": "Курс", "value": "3", "group": "Обучение"},
        ],
    }

    assert ranking["data"]["kind"] == "ranking"
    assert ranking["data"]["board"] == "Рейтинг потока"
    assert ranking["data"]["total"] == 4
    assert ranking["data"]["participants"] == 120
    assert ranking["data"]["period"] == "Осень 2026"
    assert [(r["position"], r["isMe"]) for r in ranking["data"]["rows"]] == [
        (1, False),
        (2, False),
        (3, False),
        (14, True),
    ]
    assert ranking["data"]["rows"][3] == {
        "position": 14,
        "label": "Иванова А.",
        "score": "81,2",
        "scoreNum": 81.2,
        "isMe": True,
        "total": 120,
        "period": "Осень 2026",
    }

    # a second read makes nothing new
    assert len(await _panels(v4_client)) == 4


async def test_a_student_without_data_has_no_panels(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await _student(v4_client, tx_sessionmaker, clock)
    assert await _panels(v4_client) == []


async def test_panels_are_grouped_by_university_and_can_be_filtered(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    sm = tx_sessionmaker
    user_id, first_space, first_source = await _student(v4_client, sm, clock, "Первый вуз")
    second_space = await make_space(sm, user_id, "Второй вуз")
    second_source = await make_source(
        sm, user_id=user_id, space_id=second_space, url="https://second.test/app"
    )
    await add_profile(
        sm, user_id=user_id, source_id=first_source, fields=[("group", "Группа", "ПМ-31", None)]
    )
    await add_grades(
        sm, user_id=user_id, source_id=second_source, grades=[G("Тест", "7", "10", 7.0, 10.0)]
    )
    everything = await _panels(v4_client)
    assert [(p["space"]["id"], p["key"]) for p in everything] == [
        (first_space, "student-profile"),
        (second_space, "grades-overview"),
        (second_space, "grades-chart"),
    ]
    assert everything[0]["space"]["title"] == "Первый вуз"
    only_second = await _panels(v4_client, space_id=second_space)
    assert [p["key"] for p in only_second] == ["grades-overview", "grades-chart"]
    # each university sees only its own data
    assert only_second[0]["data"]["total"] == 1
    assert everything[0]["data"]["kind"] == "profile"


async def test_another_students_panels_are_invisible_and_untouchable(
    v4_client: httpx.AsyncClient,
    new_client: Callable[[], httpx.AsyncClient],
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    sm = tx_sessionmaker
    await _full_student(v4_client, sm, clock)
    mine = await _panels(v4_client)
    assert len(mine) == 4

    other = new_client()
    other_user, other_space, other_source = await _student(other, sm, clock)
    assert await _panels(other) == []  # nothing of the first student's is shown
    await add_grades(
        sm, user_id=other_user, source_id=other_source, grades=[G("Чужая", "5", "5", 5.0, 5.0)]
    )
    theirs = await _panels(other)
    assert {p["space"]["id"] for p in theirs} == {other_space}
    assert theirs[0]["data"]["total"] == 1
    assert theirs[0]["data"]["latest"]["title"] == "Чужая"

    panel_id = mine[0]["id"]
    for call in (
        other.patch(f"/api/v1/panels/{panel_id}", json={"visible": False}),
        other.post(f"/api/v1/panels/{panel_id}/accept"),
        other.post(f"/api/v1/panels/{panel_id}/dismiss"),
        other.get("/api/v1/panels", params={"space_id": mine[0]["space"]["id"]}),
    ):
        response = await call
        assert response.status_code == 404, response.text
    after = await _panels(v4_client)
    assert [(p["id"], p["visible"], p["status"]) for p in after] == [
        (p["id"], True, "active") for p in mine
    ]


# --- the student's switches ----------------------------------------------------------------------


async def test_patch_switches_visibility_and_order(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await _full_student(v4_client, tx_sessionmaker, clock)
    panels = _by_key(await _panels(v4_client))
    overview = panels["grades-overview"]

    hidden = await v4_client.patch(
        f"/api/v1/panels/{overview['id']}", json={"visible": False, "hiddenInAll": True}
    )
    assert hidden.status_code == 200, hidden.text
    body = hidden.json()
    assert (body["visible"], body["hiddenInAll"], body["position"]) == (False, True, 0)
    assert body["data"] is None  # nothing is read for a panel that is switched off
    assert set(body) == PANEL_KEYS

    listed = _by_key(await _panels(v4_client))
    assert listed["grades-overview"]["visible"] is False
    assert listed["grades-overview"]["data"] is None
    assert listed["grades-chart"]["data"] is not None
    assert len(listed) == 4  # the switched-off panel is still listed, so the screen can offer it

    shown = await v4_client.patch(
        f"/api/v1/panels/{overview['id']}", json={"visible": True, "position": 7}
    )
    assert shown.json()["data"]["kind"] == "grades"  # data is back
    assert (shown.json()["hiddenInAll"], shown.json()["position"]) == (True, 7)  # omitted stays
    assert [p["key"] for p in await _panels(v4_client)][-1] == "grades-overview"  # moved last

    # the switches are stored on the server: the same panel as seen by a second client
    assert (await v4_client.get("/api/v1/panels")).json()[-1]["position"] == 7


@pytest.mark.parametrize(
    "body",
    [
        {"position": -1},
        {"position": 10_001},
        {"visible": "maybe"},
        {"title": "x"},
        {"status": "active"},
    ],
)
async def test_patch_rejects_what_is_not_a_switch(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, body: dict[str, Any]
) -> None:
    await _full_student(v4_client, tx_sessionmaker, clock)
    panel = (await _panels(v4_client))[0]
    response = await v4_client.patch(f"/api/v1/panels/{panel['id']}", json=body)
    assert response.status_code == 422
    assert (await _panels(v4_client))[0]["position"] == panel["position"]


async def test_proposals_are_listed_apart_and_accepted_or_dismissed(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    sm = tx_sessionmaker
    user_id, space_id, _ = await _full_student(v4_client, sm, clock)
    async with sm() as session:
        proposal = await service.propose_panel(
            session,
            user_id=user_id,
            space_id=space_id,
            key="grades-table",
            spec={
                "v": 1,
                "type": "table",
                "title": "Все оценки",
                "icon": "table",
                "dataset": {"kind": "grades"},
                "view": {"columns": ["title", "value", "date"], "limit": 2},
            },
        )
        await service.propose_panel(
            session,
            user_id=user_id,
            space_id=space_id,
            key="top-three",
            spec={
                "v": 1,
                "type": "ranking",
                "title": "Топ потока",
                "icon": "trophy",
                "dataset": {"kind": "ranking", "board": "Рейтинг потока"},
                "view": {"top": 1},
            },
        )
        await session.commit()

    assert "grades-table" not in _by_key(await _panels(v4_client))  # not shown until accepted
    proposed = _by_key(await _panels(v4_client, status="proposed"))
    assert set(proposed) == {"grades-table", "top-three"}
    table = proposed["grades-table"]
    assert (table["status"], table["visible"], table["origin"]) == ("proposed", False, "agent")
    assert len(table["data"]["points"]) == 2  # a preview, bounded by the view's limit
    # the top-1 ranking still shows the student's own place below the top
    assert [r["position"] for r in proposed["top-three"]["data"]["rows"]] == [1, 14]

    accepted = await v4_client.post(f"/api/v1/panels/{proposal.panel.id}/accept")
    assert accepted.status_code == 200, accepted.text
    assert (accepted.json()["status"], accepted.json()["visible"]) == ("active", True)
    assert accepted.json()["data"]["total"] == 4
    assert "grades-table" in _by_key(await _panels(v4_client))
    assert "grades-table" not in _by_key(await _panels(v4_client, status="proposed"))

    dismissed = await v4_client.post(f"/api/v1/panels/{proposed['top-three']['id']}/dismiss")
    assert dismissed.status_code == 200, dismissed.text
    assert (dismissed.json()["status"], dismissed.json()["data"]) == ("dismissed", None)
    assert "top-three" not in _by_key(await _panels(v4_client, status="proposed"))
    assert "top-three" not in _by_key(await _panels(v4_client))
    # a dismissed panel cannot be switched on by PATCH; accepting brings it back
    assert (
        await v4_client.patch(f"/api/v1/panels/{dismissed.json()['id']}", json={"visible": True})
    ).status_code == 409
    back = await v4_client.post(f"/api/v1/panels/{dismissed.json()['id']}/accept")
    assert back.status_code == 200
    # an active panel cannot go back to proposed or be accepted twice with a different meaning
    again = await v4_client.post(f"/api/v1/panels/{accepted.json()['id']}/accept")
    assert again.status_code == 200
    assert (await v4_client.post("/api/v1/panels/missing/accept")).status_code == 404


async def test_a_panel_with_a_broken_stored_spec_is_skipped_not_a_500(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    sm = tx_sessionmaker
    await _full_student(v4_client, sm, clock)
    panel = (await _panels(v4_client))[0]
    async with sm() as session:
        await session.execute(
            update(Panel)
            .where(Panel.id == panel["id"])
            .values(spec={"v": 9, "type": "kpi", "title": "<b>x</b>"})
        )
        await session.commit()
    keys = [p["key"] for p in await _panels(v4_client)]
    assert panel["key"] not in keys
    assert len(keys) == 3
    assert (
        await v4_client.patch(f"/api/v1/panels/{panel['id']}", json={"visible": False})
    ).status_code == 404


# --- bounded data ------------------------------------------------------------------------------


async def _propose_active(
    client: httpx.AsyncClient, sm: Sm, user_id: str, space_id: str, key: str, spec: dict[str, Any]
) -> dict[str, Any]:
    async with sm() as session:
        proposal = await service.propose_panel(
            session, user_id=user_id, space_id=space_id, key=key, spec=spec
        )
        await session.commit()
    response = await client.post(f"/api/v1/panels/{proposal.panel.id}/accept")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _spec(kind: str, view: dict[str, Any], dataset: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "v": 1,
        "type": kind,
        "title": "Панель",
        "icon": "chart",
        "dataset": dataset or {"kind": "grades"},
        "view": view,
    }


async def test_the_data_of_a_view_is_cut_by_its_limits(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    sm = tx_sessionmaker
    user_id, space_id, source_id = await _student(v4_client, sm, clock)
    scales = [
        (5.0, "5"),
        (10.0, "10"),
        (20.0, "20"),
        (50.0, "50"),
        (100.0, "100"),
        (200.0, "200"),
        (500.0, "500"),
    ]
    grades: list[G] = []
    for day in range(1, 29):  # 28 dated numeric grades over seven scales
        top, scale = scales[day % len(scales)]
        grades.append(G(f"Оценка {day}", "1", scale, 1.0, top, at(day)))
    grades.append(G("Без даты", "2", "10", 2.0, 10.0))
    await add_grades(sm, user_id=user_id, source_id=source_id, grades=grades)

    table = await _propose_active(
        v4_client, sm, user_id, space_id, "t", _spec("table", {"columns": ["title"], "limit": 5})
    )
    assert table["data"]["total"] == 29
    assert len(table["data"]["points"]) == 5
    assert [p["title"] for p in table["data"]["points"]][:2] == ["Оценка 28", "Оценка 27"]  # newest

    listed = await _propose_active(
        v4_client, sm, user_id, space_id, "l", _spec("list", {"limit": 3})
    )
    assert len(listed["data"]["points"]) == 3

    chart = await _propose_active(
        v4_client, sm, user_id, space_id, "c", _spec("chart", {"x": "date", "limit": 50})
    )
    points = chart["data"]["points"]
    assert len({p["scaleMax"] for p in points}) <= 6  # at most six series
    assert all(p["valueNum"] is not None and p["gradedAt"] for p in points)  # numeric and dated
    assert [p["gradedAt"] for p in points] == sorted(p["gradedAt"] for p in points)  # left to right
    assert len(points) <= 50

    short = await _propose_active(
        v4_client, sm, user_id, space_id, "c2", _spec("chart", {"x": "date", "limit": 4})
    )
    assert len(short["data"]["points"]) == 4
    # the four newest of the busiest scales, still oldest first
    assert short["data"]["points"][-1]["gradedAt"] >= short["data"]["points"][0]["gradedAt"]


async def test_a_course_dataset_shows_only_that_course(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    sm = tx_sessionmaker
    user_id, space_id, source_id = await _student(v4_client, sm, clock)
    math = await add_course(
        sm, user_id=user_id, source_id=source_id, external_id="m", title="Математика"
    )
    await add_course(sm, user_id=user_id, source_id=source_id, external_id="p", title="Физика")
    await add_grades(
        sm,
        user_id=user_id,
        source_id=source_id,
        grades=[
            G("Матан-1", "8", "10", 8.0, 10.0, at(1), "m"),
            G("Матан-2", "9", "10", 9.0, 10.0, at(2), "m"),
            G("Физика-1", "5", "10", 5.0, 10.0, at(3), "p"),
        ],
    )
    panel = await _propose_active(
        v4_client,
        sm,
        user_id,
        space_id,
        "math",
        _spec("table", {}, {"kind": "grades", "courseId": math}),
    )
    assert panel["data"]["total"] == 2
    assert {p["course"] for p in panel["data"]["points"]} == {"Математика"}
    assert {p["courseId"] for p in panel["data"]["points"]} == {math}


async def test_ranking_and_profile_views_pick_and_order_rows(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    sm = tx_sessionmaker
    user_id, space_id, _ = await _full_student(v4_client, sm, clock)
    plain = await _propose_active(
        v4_client,
        sm,
        user_id,
        space_id,
        "top2",
        _spec(
            "ranking",
            {"top": 2, "highlightMe": False},
            {"kind": "ranking", "board": "Рейтинг потока"},
        ),
    )
    assert [r["position"] for r in plain["data"]["rows"]] == [1, 2]  # no extra row for the student
    assert plain["data"]["total"] == 4
    missing = await _propose_active(
        v4_client,
        sm,
        user_id,
        space_id,
        "nothing",
        _spec("ranking", {}, {"kind": "ranking", "board": "Нет такого списка"}),
    )
    assert missing["data"]["rows"] == []
    assert missing["data"]["total"] == 0

    ordered = await _propose_active(
        v4_client,
        sm,
        user_id,
        space_id,
        "card",
        _spec("profile", {"fields": ["year", "unknown", "GROUP"]}, {"kind": "profile"}),
    )
    assert [f["field"] for f in ordered["data"]["fields"]] == ["year", "group"]
    assert ordered["data"]["total"] == 3


async def test_a_switched_off_source_hides_its_data_from_panels(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    sm = tx_sessionmaker
    _, _, source_id = await _full_student(v4_client, sm, clock)
    assert len(await _panels(v4_client)) == 4
    switched = await v4_client.post(f"/api/v1/sources/{source_id}/disable")
    assert switched.status_code in (200, 204), switched.text
    panels = _by_key(await _panels(v4_client))
    # the panels stay (the student's choices are kept) but show nothing: the data is hidden
    assert panels["grades-overview"]["data"]["total"] == 0
    assert panels["student-profile"]["data"]["fields"] == []
