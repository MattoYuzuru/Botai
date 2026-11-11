"""`study_data`, `study_status_rule`, `study_mark_task` and the study digest (D-044, D-045).

The tools are driven directly with a `ToolContext` over a real Postgres transaction with rows of
two students: what comes back for a filter, how small and linked it is, how fresh each source is,
that the other student's rows never appear, and that a correction changes the data at once and
says how many tasks it touched. The digest is checked for content, its cap and that a failing read
skips it instead of failing the turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update

from botai.agent.v4 import study_context
from botai.agent.v4.protocol import ToolContext, ToolHandler, ToolResult, validate_arguments
from botai.agent.v4.study_context import CUT_LINE, DIGEST_CHARS, safe_study_digest, study_digest
from botai.agent.v4.tools.study import StudyToolkit, build_study_tools
from botai.core.clock import FrozenClock
from botai.db.models import Course, Deadline, Grade, ScheduleEvent, Source
from tests.auth.support import *  # noqa: F403
from tests.auth.support import Sm
from tests.panels.support import BASE, make_source
from tests.sources.v4.support import Account, create_account

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)  # 12:00 in Moscow, a Thursday
FOREIGN_VALUE = "ЧУЖАЯ-ОЦЕНКА-42"


def _row(user_id: str, source_id: str, external_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "connection_id": None,
        "source_id": source_id,
        "external_id": external_id,
        "source_url": f"{BASE}/api",
        "open_url": f"{BASE}/learn",
        "fetched_at": NOW,
        "seen_at": NOW,
    }


@dataclass(frozen=True, slots=True)
class Seeded:
    account: Account
    other: Account
    source_id: str
    other_source_id: str
    deadlines: dict[str, str]  # title -> id
    other_deadline_id: str


async def _seed(sm: Sm) -> Seeded:
    account = await create_account(sm)
    other = await create_account(sm, title="Другой вуз")
    source_id = await make_source(
        sm, user_id=account.user_id, space_id=account.space_id, title="ЛК"
    )
    other_source_id = await make_source(sm, user_id=other.user_id, space_id=other.space_id)
    deadlines: dict[str, str] = {}
    async with sm() as session:
        await session.execute(
            update(Source)
            .where(Source.id == source_id)
            .values(status="active", last_sync_at=NOW - timedelta(minutes=20))
        )
        await session.execute(
            update(Source)
            .where(Source.id == other_source_id)
            .values(status="active", last_sync_at=NOW)
        )
        row = _row(account.user_id, source_id, "")
        for ext, title in (("alg", "Алгебра"), ("hist", "История")):
            session.add(Course(title=title, archived=False, **{**row, "external_id": ext}))
        session.add_all(
            [
                Grade(
                    title="ДЗ 7",
                    value="8",
                    value_num=8,
                    scale_max=10,
                    layer="item",
                    course_external_id="alg",
                    graded_at=NOW - timedelta(days=1),
                    changed_at=NOW - timedelta(hours=1),
                    url=f"{BASE}/learn/tasks/7",
                    comment="Хорошо, но " + "очень " * 200 + "длинно",
                    **{**row, "external_id": "g-7"},
                ),
                Grade(
                    title="Накопленная",
                    value="7,5",
                    layer="current",
                    course_external_id="alg",
                    **{**row, "external_id": "g-acc"},
                ),
                Grade(
                    title="ДЗ 1",
                    value="5",
                    layer="item",
                    course_external_id="hist",
                    **{**row, "external_id": "g-h1"},
                ),
            ]
        )
        for ext, title, due, state, words in (
            ("d8", "ДЗ 8", NOW + timedelta(days=2), "open", "Не начато"),
            ("d6", "ДЗ 6", NOW - timedelta(days=5), "submitted", "Принято"),
            ("d1", "ДЗ 1 старое", NOW - timedelta(days=60), "graded", "Проверено"),
            ("d0", "ДЗ 0 просрочено", NOW - timedelta(days=30), "open", "Не начато"),
            ("d9", "ДЗ 9", NOW + timedelta(days=3), "unknown", "Жёлтая метка"),
        ):
            deadline = Deadline(
                title=title,
                course_external_id="alg",
                due_at=due,
                state=state,
                state_text=words,
                url=f"{BASE}/learn/tasks/{ext}",
                **{**row, "external_id": ext},
            )
            session.add(deadline)
            await session.flush()
            deadlines[title] = deadline.id
        for ext, title, starts in (
            ("e-today", "Алгебра: семинар", NOW + timedelta(hours=2)),
            ("e-later", "Алгебра: лекция", NOW + timedelta(days=10)),
        ):
            session.add(
                ScheduleEvent(
                    title=title,
                    course_external_id="alg",
                    starts_at=starts,
                    ends_at=starts + timedelta(minutes=90),
                    location_text="ауд. 305",
                    **{**row, "external_id": ext},
                )
            )
        foreign = _row(other.user_id, other_source_id, "")
        session.add(
            Grade(
                title="ДЗ 7",
                value=FOREIGN_VALUE,
                layer="item",
                changed_at=NOW,
                **{**foreign, "external_id": "x-7"},
            )
        )
        other_deadline = Deadline(
            title="Чужое ДЗ",
            due_at=NOW + timedelta(days=1),
            state="unknown",
            state_text="Жёлтая метка",
            **{**foreign, "external_id": "x-d"},
        )
        session.add(other_deadline)
        await session.flush()
        other_deadline_id = other_deadline.id
        await session.commit()
    return Seeded(account, other, source_id, other_source_id, deadlines, other_deadline_id)


@pytest.fixture(name="seeded")
async def _seeded(tx_sessionmaker: Sm) -> Seeded:
    return await _seed(tx_sessionmaker)


def _tools(sm: Sm) -> dict[str, ToolHandler]:
    toolkit = StudyToolkit(sm=sm, clock=FrozenClock(NOW))
    return {tool.spec.name: tool for tool in build_study_tools(toolkit)}


async def _run(
    tools: dict[str, ToolHandler], name: str, args: dict[str, Any], user: str
) -> ToolResult:
    assert validate_arguments(tools[name].spec, args) == [], (
        "the schema admits what the model sends"
    )
    return await tools[name].run(args, ToolContext(run_id="run-1", user_id=user))


# --- study_data ---------------------------------------------------------------------------------


async def test_grades_are_filtered_compact_linked_and_fresh(
    tx_sessionmaker: Sm, seeded: Seeded
) -> None:
    tools = _tools(tx_sessionmaker)
    result = await _run(
        tools,
        "study_data",
        {"kinds": ["grades"], "course": "алгеб", "layer": "item"},
        seeded.account.user_id,
    )

    assert result.ok, result.error
    section = result.data["grades"]
    assert section["total"] == 1
    assert section["truncated"] is False
    (grade,) = section["items"]
    assert grade["title"] == "ДЗ 7"
    assert grade["courseTitle"] == "Алгебра"
    assert (grade["value"], grade["scaleMax"], grade["layer"]) == ("8", 10, "item")
    assert grade["url"] == f"{BASE}/learn/tasks/7", "the link is the work's own page"
    assert len(grade["comment"]) <= 300, "long texts are cut"
    assert "provenance" not in grade
    (source,) = result.data["sources"]
    assert source["id"] == seeded.source_id
    assert source["ageMinutes"] == 20
    assert source["status"] == "active"
    # The other student's grade with the same title never comes back.
    assert FOREIGN_VALUE not in json.dumps(result.data, ensure_ascii=False)


async def test_deadlines_default_to_open_and_recent_done_work(
    tx_sessionmaker: Sm, seeded: Seeded
) -> None:
    tools = _tools(tx_sessionmaker)
    result = await _run(tools, "study_data", {"kinds": ["deadlines"]}, seeded.account.user_id)

    assert result.ok, result.error
    titles = [item["title"] for item in result.data["deadlines"]["items"]]
    assert set(titles) == {"ДЗ 0 просрочено", "ДЗ 8", "ДЗ 9", "ДЗ 6"}, "old done work stays out"
    nine = next(item for item in result.data["deadlines"]["items"] if item["title"] == "ДЗ 9")
    assert nine["stateText"] == "Жёлтая метка"
    assert nine["url"].endswith("/tasks/d9")
    assert "Чужое ДЗ" not in titles

    graded = await _run(
        tools, "study_data", {"kinds": ["deadlines"], "state": "graded"}, seeded.account.user_id
    )
    assert [item["title"] for item in graded.data["deadlines"]["items"]] == ["ДЗ 1 старое"]


async def test_schedule_defaults_to_the_coming_week_and_honours_a_window(
    tx_sessionmaker: Sm, seeded: Seeded
) -> None:
    tools = _tools(tx_sessionmaker)
    week = await _run(tools, "study_data", {"kinds": ["schedule"]}, seeded.account.user_id)
    assert [item["title"] for item in week.data["schedule"]["items"]] == ["Алгебра: семинар"]

    later = await _run(
        tools,
        "study_data",
        {"kinds": ["schedule", "courses"], "from": "2026-11-20", "to": "2026-11-23"},
        seeded.account.user_id,
    )
    assert [item["title"] for item in later.data["schedule"]["items"]] == ["Алгебра: лекция"]
    assert {item["title"] for item in later.data["courses"]["items"]} == {"Алгебра", "История"}


async def test_limit_truncates_and_updates_list_recent_changes(
    tx_sessionmaker: Sm, seeded: Seeded
) -> None:
    tools = _tools(tx_sessionmaker)
    result = await _run(
        tools, "study_data", {"kinds": ["grades", "updates"], "limit": 1}, seeded.account.user_id
    )
    assert result.ok, result.error
    assert result.data["grades"]["total"] == 3
    assert len(result.data["grades"]["items"]) == 1
    assert result.data["grades"]["truncated"] is True
    updates = result.data["updates"]["items"]
    assert updates
    assert updates[0]["title"] == "ДЗ 7"
    assert FOREIGN_VALUE not in json.dumps(result.data, ensure_ascii=False)


async def test_a_foreign_space_and_a_bad_date_are_structured_errors(
    tx_sessionmaker: Sm, seeded: Seeded
) -> None:
    tools = _tools(tx_sessionmaker)
    foreign = await _run(
        tools,
        "study_data",
        {"kinds": ["grades"], "space_id": seeded.other.space_id},
        seeded.account.user_id,
    )
    assert not foreign.ok
    assert foreign.error is not None
    assert foreign.error.code == "not_found"

    bad = await _run(
        tools, "study_data", {"kinds": ["schedule"], "from": "завтра"}, seeded.account.user_id
    )
    assert not bad.ok
    assert bad.error is not None
    assert bad.error.code == "invalid_arguments"


# --- corrections --------------------------------------------------------------------------------


async def test_a_status_rule_reevaluates_the_tasks_and_can_be_removed(
    tx_sessionmaker: Sm, seeded: Seeded
) -> None:
    tools = _tools(tx_sessionmaker)
    user = seeded.account.user_id
    made = await _run(
        tools,
        "study_status_rule",
        {
            "action": "set",
            "source_id": seeded.source_id,
            "label": "Жёлтая  метка",
            "state": "submitted",
        },
        user,
    )
    assert made.ok, made.error
    assert (made.data["tasks"], made.data["changed"]) == (1, 1)
    async with tx_sessionmaker() as session:
        mine = await session.get(Deadline, seeded.deadlines["ДЗ 9"])
        theirs = await session.get(Deadline, seeded.other_deadline_id)
    assert mine is not None
    assert mine.state == "submitted"
    assert theirs is not None
    assert theirs.state == "unknown", "another student's task is untouched"

    listed = await _run(tools, "study_status_rule", {"action": "list"}, user)
    assert [rule["ruleId"] for rule in listed.data["rules"]] == [made.data["ruleId"]]
    assert (await _run(tools, "study_status_rule", {"action": "list"}, seeded.other.user_id)).data[
        "rules"
    ] == []

    removed = await _run(
        tools, "study_status_rule", {"action": "delete", "rule_id": made.data["ruleId"]}, user
    )
    assert removed.ok, removed.error
    assert removed.data["changed"] == 1
    missing = await _run(tools, "study_status_rule", {"action": "set", "label": "x"}, user)
    assert not missing.ok


async def test_marking_a_task_overrides_the_source_until_cleared(
    tx_sessionmaker: Sm, seeded: Seeded
) -> None:
    tools = _tools(tx_sessionmaker)
    user = seeded.account.user_id
    task = seeded.deadlines["ДЗ 8"]
    marked = await _run(tools, "study_mark_task", {"deadline_id": task, "state": "submitted"}, user)
    assert marked.ok, marked.error
    assert marked.data["state"] == "submitted"
    assert marked.data["stateSource"] == "student"

    shown = await _run(tools, "study_data", {"kinds": ["deadlines"], "query": "дз 8"}, user)
    (item,) = shown.data["deadlines"]["items"]
    assert (item["state"], item["stateSource"]) == ("submitted", "student")

    cleared = await _run(tools, "study_mark_task", {"deadline_id": task, "state": None}, user)
    assert cleared.ok
    assert cleared.data["state"] == "open"

    foreign = await _run(
        tools,
        "study_mark_task",
        {"deadline_id": seeded.other_deadline_id, "state": "submitted"},
        user,
    )
    assert not foreign.ok
    assert foreign.error is not None
    assert foreign.error.code == "not_found"


# --- the digest ---------------------------------------------------------------------------------


async def test_the_digest_shows_sources_deadlines_changes_and_layers(
    tx_sessionmaker: Sm, seeded: Seeded
) -> None:
    async with tx_sessionmaker() as session:
        digest = await study_digest(
            session, seeded.account.user_id, now=NOW, space_id=seeded.account.space_id
        )

    assert len(digest) <= DIGEST_CHARS
    assert f"space_id={seeded.account.space_id} — к нему относится этот чат" in digest
    assert (
        f"источник «ЛК» (site, grades) — active, обновлено 20 мин назад; source_id={seeded.source_id}"
        in digest
    )
    assert "ближайшие сроки: «ДЗ 8» (Алгебра) до 14.11 12:00 — open «Не начато»" in digest
    assert "«ДЗ 6»" not in digest.split("ближайшие сроки:")[1].split("\n")[0], (
        "done work is not due"
    )
    assert "ДЗ 0 просрочено" not in digest, "a long-overdue task is not «ближайший»"
    assert "новое за 7 дн: оценка «ДЗ 7» (Алгебра): 8/10" in digest
    assert "оценок по слоям: item 2, current 1" in digest
    assert FOREIGN_VALUE not in digest
    assert "Чужое ДЗ" not in digest


async def test_the_digest_is_capped_and_says_so(tx_sessionmaker: Sm, seeded: Seeded) -> None:
    async with tx_sessionmaker() as session:
        for index in range(40):
            session.add(
                Deadline(
                    title=f"Очень длинное название задания номер {index} " + "x" * 60,
                    due_at=NOW + timedelta(hours=index + 1),
                    state="open",
                    state_text="В работе",
                    **_row(seeded.account.user_id, seeded.source_id, f"bulk-{index}"),
                )
            )
        for index in range(12):
            await make_source(
                tx_sessionmaker,
                user_id=seeded.account.user_id,
                space_id=seeded.account.space_id,
                title=f"Источник {index} " + "y" * 50,
                url=f"{BASE}/extra/{index}",
            )
        await session.commit()
    async with tx_sessionmaker() as session:
        digest = await study_digest(session, seeded.account.user_id, now=NOW)
    assert len(digest) <= DIGEST_CHARS
    assert digest.endswith(CUT_LINE)
    assert f"source_id={seeded.source_id}" in digest, "the ids come before the details"
    async with tx_sessionmaker() as session:
        again = await study_digest(session, seeded.account.user_id, now=NOW)
    assert again == digest, "deterministic"


async def test_a_failing_read_skips_the_digest_without_breaking_the_session(
    tx_sessionmaker: Sm,
    seeded: Seeded,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> list[Any]:
        del args, kwargs
        raise RuntimeError("secret detail that must not be logged")

    monkeypatch.setattr(study_context.repo, "list_updates", broken)
    async with tx_sessionmaker() as session:
        digest = await safe_study_digest(session, seeded.account.user_id, now=NOW)
        assert digest == ""
        assert await session.scalar(select(text("1"))) == 1, "the session is still usable"
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text
