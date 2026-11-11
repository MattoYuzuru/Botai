"""The student's corrections of task statuses and the «Новое» feed over the API (D-045, §5.5).

Rows are written through the ORM (the sync path is `tests/sources/v4/test_grades_status_flow.py`);
what is proven here is the effective state, the rules' recomputation, the grade of a task, the
updates feed and the ownership of every route.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from botai.core.errors import BotaiError
from botai.db.models import Deadline, SourcePriority
from botai.domain import overrides
from botai.sources.v4 import service as v4_service
from tests.auth.support import Sm
from tests.domain.helpers import T0, World, add, at
from tests.domain.test_read_path_v4 import v4_deadline, v4_grade

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
    async with sm() as session:
        source = await v4_service.create_source(
            session,
            user_id=user_id,
            space_id=space_id,
            title=title,
            entry_url=url,
            kinds=["site", "deadlines", "grades"],
            status=status,
        )
        await session.commit()
        return source.id


async def _get(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _deadlines(client: httpx.AsyncClient, *, done: bool = False) -> dict[str, Any]:
    params = {"include_done": "true"} if done else {}
    return {d["title"]: d for d in await _get(client, "/api/v1/deadlines", **params)}


async def test_the_student_marks_a_task_and_clears_the_mark(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    task = v4_deadline(w.user_id, source, at(12, day=30), title="ДЗ 1", state_text="Задано")
    await add(tx_sessionmaker, task)
    shown = (await _deadlines(w.client))["ДЗ 1"]
    assert (shown["state"], shown["stateText"], shown["stateSource"]) == (
        "open",
        "Задано",
        "source",
    )
    assert shown["grade"] is None
    assert shown["changedAt"] is None

    response = await w.client.patch(f"/api/v1/deadlines/{task.id}", json={"userState": "submitted"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["state"], body["stateSource"]) == ("submitted", "student")
    assert body["changedAt"] == "2026-10-27T12:00:00Z"
    assert "ДЗ 1" not in await _deadlines(w.client)  # done for the student: off the list
    assert (await _deadlines(w.client, done=True))["ДЗ 1"]["stateSource"] == "student"

    cleared = await w.client.patch(f"/api/v1/deadlines/{task.id}", json={"userState": None})
    assert cleared.json()["state"] == "open"
    assert cleared.json()["stateSource"] == "source"

    bad = await w.client.patch(f"/api/v1/deadlines/{task.id}", json={"userState": "missed"})
    assert bad.status_code == 422
    missing = await w.client.patch("/api/v1/deadlines/nope", json={"userState": "open"})
    assert missing.status_code == 404


async def test_a_mark_on_one_copy_decides_the_task_in_every_source(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    primary = await _v4_source(tx_sessionmaker, w.user_id, w.space_a, title="ЛК")
    other = await _v4_source(
        tx_sessionmaker, w.user_id, w.space_a, title="Календарь", url="https://fakeuni.test/cal"
    )
    async with tx_sessionmaker() as session:
        session.add(
            SourcePriority(space_id=w.space_a, kind="deadlines", source_ids=[primary, other])
        )
        await session.commit()
    copy = v4_deadline(w.user_id, other, at(12, day=30), title="Курсовая", user_state="submitted")
    await add(
        tx_sessionmaker, v4_deadline(w.user_id, primary, at(12, day=30), title="Курсовая"), copy
    )
    assert "Курсовая" not in await _deadlines(w.client)
    kept = (await _deadlines(w.client, done=True))["Курсовая"]
    assert kept["provenance"]["sourceId"] == primary  # the primary's row is shown
    assert (kept["state"], kept["stateSource"]) == ("submitted", "student")


async def test_status_rules_recompute_the_stored_tasks_at_once(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    other = await _v4_source(
        tx_sessionmaker, w.user_id, w.space_b, title="Другой", url="https://other.test/app"
    )
    await add(
        tx_sessionmaker,
        v4_deadline(
            w.user_id, source, at(12, day=29), title="A", state="unknown", state_text="Решаю"
        ),
        v4_deadline(
            w.user_id, other, at(12, day=30), title="B", state="unknown", state_text="решаю"
        ),
    )
    created = await w.client.post(
        "/api/v1/status-rules", json={"sourceId": source, "label": "  РЕШАЮ ", "state": "submitted"}
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    assert {k: rule[k] for k in ("sourceId", "sourceTitle", "label", "state")} == {
        "sourceId": source,
        "sourceTitle": "ЛК Fakeuni",
        "label": "решаю",
        "state": "submitted",
    }
    tasks = await _deadlines(w.client, done=True)
    assert (tasks["A"]["state"], tasks["A"]["stateSource"]) == ("submitted", "rule")
    assert (tasks["B"]["state"], tasks["B"]["stateSource"]) == ("unknown", "source")  # other source
    assert set(await _deadlines(w.client)) == {"B"}

    # a rule for every source; the source's own rule still wins inside its source
    everywhere = await w.client.post(
        "/api/v1/status-rules", json={"sourceId": None, "label": "решаю", "state": "open"}
    )
    assert everywhere.status_code == 201
    tasks = await _deadlines(w.client, done=True)
    assert (tasks["A"]["state"], tasks["B"]["state"]) == ("submitted", "open")
    assert tasks["B"]["stateSource"] == "rule"

    # the same label again replaces the rule, never duplicates it
    again = await w.client.post(
        "/api/v1/status-rules", json={"sourceId": source, "label": "Решаю", "state": "graded"}
    )
    assert again.json()["id"] == rule["id"]
    listed = await _get(w.client, "/api/v1/status-rules")
    assert sorted((r["sourceId"] or "", r["state"]) for r in listed) == [
        ("", "open"),
        (source, "graded"),
    ]

    deleted = await w.client.delete(f"/api/v1/status-rules/{rule['id']}")
    assert deleted.status_code == 204
    tasks = await _deadlines(w.client, done=True)
    assert (tasks["A"]["state"], tasks["A"]["stateSource"]) == (
        "open",
        "rule",
    )  # the all-sources one
    assert (await w.client.delete(f"/api/v1/status-rules/{rule['id']}")).status_code == 404

    foreign = await w.client.post(
        "/api/v1/status-rules", json={"sourceId": "nope", "label": "x", "state": "open"}
    )
    assert foreign.status_code == 404
    unknown = await w.client.post("/api/v1/status-rules", json={"label": "x", "state": "unknown"})
    assert unknown.status_code == 422


async def test_rules_and_marks_belong_to_their_owner(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    task = v4_deadline(w.user_id, source, at(12, day=30), state_text="Решаю", state="unknown")
    await add(tx_sessionmaker, task)
    async with tx_sessionmaker() as session:
        rule = await overrides.set_status_rule(
            session, w.user_id, source_id=source, label="Решаю", state="submitted"
        )
        await session.commit()
        stranger = "01STRANGER0000000000000000"
        for call in (
            overrides.set_user_state(session, stranger, task.id, "submitted"),
            overrides.delete_status_rule(session, stranger, rule.id),
            overrides.set_status_rule(session, stranger, source_id=source, label="x", state="open"),
        ):
            with pytest.raises(BotaiError):
                await call
        assert await overrides.list_status_rules(session, stranger) == []
        assert (
            await session.scalar(select(Deadline.user_state).where(Deadline.id == task.id)) is None
        )


async def test_a_task_shows_the_latest_grade_of_its_work(world: World, tx_sessionmaker: Sm) -> None:
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    await add(
        tx_sessionmaker,
        v4_deadline(
            w.user_id,
            source,
            at(12, day=29),
            title="ДЗ 1",
            ext="t-1",
            assignment_external_id="ex-1",
        ),
        v4_deadline(w.user_id, source, at(12, day=30), title="ДЗ 2", ext="t-2"),
        v4_grade(w.user_id, source, "5", assignment_external_id="ex-1", graded_at=at(9, day=20)),
        v4_grade(
            w.user_id,
            source,
            "8",
            scale="из 10",
            value_num=8.0,
            scale_max=10.0,
            assignment_external_id="ex-1",
            graded_at=at(9, day=25),
            url="https://fakeuni.test/tasks/ex-1",
        ),
        v4_grade(
            w.user_id, source, "зачтено", assignment_external_id="t-2"
        ),  # by the task's own id
    )
    tasks = await _deadlines(w.client)
    assert tasks["ДЗ 1"]["grade"] == {
        "value": "8",
        "scale": "из 10",
        "valueNum": 8.0,
        "scaleMax": 10.0,
        "url": "https://fakeuni.test/tasks/ex-1",
    }
    assert tasks["ДЗ 2"]["grade"]["value"] == "зачтено"
    grades = await _get(w.client, "/api/v1/grades")
    newest = next(g for g in grades if g["value"] == "8")
    assert {k: newest[k] for k in ("assignmentId", "url", "layer", "period", "comment")} == {
        "assignmentId": "ex-1",
        "url": "https://fakeuni.test/tasks/ex-1",
        "layer": None,
        "period": None,
        "comment": None,
    }


async def test_updates_are_recent_changes_of_live_sources_newest_first(
    world: World, tx_sessionmaker: Sm
) -> None:
    w = world
    source = await _v4_source(tx_sessionmaker, w.user_id, w.space_a)
    off = await _v4_source(
        tx_sessionmaker,
        w.user_id,
        w.space_b,
        title="Выключен",
        url="https://off.test",
        status="disabled",
    )
    await add(
        tx_sessionmaker,
        v4_grade(
            w.user_id,
            source,
            "9",
            title="Контрольная",
            scale="из 10",
            value_num=9.0,
            scale_max=10.0,
            layer="item",
            changed_at=T0 - timedelta(hours=2),
        ),
        v4_deadline(
            w.user_id,
            source,
            at(12, day=30),
            title="ДЗ 3",
            state="graded",
            state_text="Принято",
            changed_at=T0 - timedelta(days=1),
        ),
        v4_grade(w.user_id, source, "7", title="Старое", changed_at=T0 - timedelta(days=10)),
        v4_grade(w.user_id, source, "6", title="Без изменений"),
        v4_grade(w.user_id, off, "3", title="Скрыто", changed_at=T0 - timedelta(hours=1)),
    )
    updates = await _get(w.client, "/api/v1/updates", days=7)
    assert [(u["kind"], u["title"]) for u in updates] == [
        ("grade", "Контрольная"),
        ("deadline", "ДЗ 3"),
    ]
    assert {k: updates[0][k] for k in ("value", "scaleMax", "layer", "state")} == {
        "value": "9",
        "scaleMax": 10.0,
        "layer": "item",
        "state": None,
    }
    assert (updates[1]["state"], updates[1]["stateText"]) == ("graded", "Принято")
    assert [u["title"] for u in await _get(w.client, "/api/v1/updates", days=30)] == [
        "Контрольная",
        "ДЗ 3",
        "Старое",
    ]
    assert await _get(w.client, "/api/v1/updates", space_id=w.space_b) == []
    assert (await w.client.get("/api/v1/updates", params={"days": 0})).status_code == 422
    today = await _get(w.client, "/api/v1/today")
    assert [u["title"] for u in today["updates"]] == ["Контрольная", "ДЗ 3"]
