"""A cabinet whose "my classes" page shows RULES: research, canary, sync and the domain (L1).

The synthetic SPA (`tests/fakeuni/lms.py`) answers the rules of every course
(`/api/lms/students/me/timetables`, a day of the week as a word) and the tasks behind a query whose
key repeats (`?state=a&state=b...`). The author's answers below are the ones the real model gave
on this stand (L2, `test_lms_live.py`), minus what is not data: no secret, no student value.
Nothing here reaches a real university or a real model.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from botai.core.llm import LlmRequest, LlmResponse
from botai.db import models as m
from botai.domain import repo
from tests.fakeuni import lms, login
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.learn.lms_env import ENTRY, scripted_lms_site
from tests.learn.test_probe import plan
from tests.sources.helpers import NOW

MONDAY_LECTURE = "0b6c2f10-6f0e-4c3a-9a51-1f4d3e9b7a01"  # Линейная алгебра, monday 09:30
BIWEEKLY_SEMINAR = (
    "0b6c2f10-6f0e-4c3a-9a51-1f4d3e9b7a04"  # Основы баз данных, friday, every 2nd week
)
THURSDAY_LECTURE = "0b6c2f10-6f0e-4c3a-9a51-1f4d3e9b7a05"  # Алгоритмы, thursday 09:30


# --- what the author wrote, per kind -----------------------------------------------------------


def schedule_endpoint() -> dict[str, Any]:
    row = {"from": "row"}
    return {
        "id": "timetable",
        "kind": "scheduleEvents",
        "request": {"method": "GET", "path": lms.TIMETABLES_PATH},
        "response": {
            "format": "json",
            "collections": [{"as": "course", "path": ""}, {"as": "row", "path": "/eventRows"}],
            "fields": {
                "title": {"from": "course", "path": "/courseName", "required": True},
                "courseId": {"from": "course", "path": "/courseId"},
                "externalId": {**row, "path": "/calendarEvent/calendarEventId", "required": True},
                "kind": {**row, "path": "/calendarEvent/eventType"},
                "format": {**row, "path": "/calendarEvent/format"},
                "location": {**row, "path": "/calendarEvent/location/title"},
                "teacher": {**row, "path": "/calendarEvent/hosts", "transform": "joinNames"},
            },
            "attributes": [{**row, "label": "Корпус", "path": "/calendarEvent/location/building"}],
            "expand": {
                "type": "weekly",
                "timeZone": "Europe/Moscow",
                "horizon": {"from": "{weekStart}", "days": 90},
                "startDate": {**row, "path": "/calendarEvent/schedule/startDate"},
                "untilDate": {**row, "path": "/calendarEvent/schedule/endDate"},
                "weekdays": {**row, "path": "/calendarEvent/schedule/dayOfWeek"},
                "startTime": {**row, "path": "/calendarEvent/schedule/startTime"},
                "endTime": {**row, "path": "/calendarEvent/schedule/endTime"},
                "intervalWeeks": {**row, "path": "/calendarEvent/schedule/interval"},
            },
        },
    }


def deadlines_endpoint(states: Any = tuple(lms.STATES)) -> dict[str, Any]:
    query: dict[str, Any] = {"state": list(states) if isinstance(states, tuple) else states}
    return {
        "id": "deadlines",
        "kind": "deadlines",
        "request": {"method": "GET", "path": lms.TASKS_PATH, "query": query},
        "response": {
            "format": "json",
            "itemsPath": "",
            "fields": {
                "externalId": {"path": "/id", "required": True},
                "assignmentId": {"path": "/exercise/id"},
                "courseId": {"path": "/course/id"},
                "title": {"path": "/exercise/name", "required": True},
                "dueAt": {"path": "/deadline", "transform": "iso", "required": True},
                "state": {"path": "/state"},
            },
        },
    }


def courses_endpoint() -> dict[str, Any]:
    return {
        "id": "courses",
        "kind": "courses",
        "request": {"method": "GET", "path": lms.TIMETABLES_PATH},
        "response": {
            "format": "json",
            "itemsPath": "",
            "fields": {
                "externalId": {"path": "/courseId", "required": True},
                "title": {"path": "/courseName", "required": True},
            },
        },
    }


KINDS = ["scheduleEvents", "deadlines", "courses"]


def author(
    *endpoints: dict[str, Any], bootstrap: list[dict[str, Any]] | None = None
) -> LlmResponse:
    return ls.answer(list(endpoints), bootstrap=bootstrap)


def planner() -> list[LlmResponse]:
    """Look at the timetable, open the tasks page through the menu, then hand over."""
    return [
        plan(1, "navigate", "l0", kinds=["scheduleEvents"], evidence=["p0", "r2"]),
        plan(2, "finish", kinds=KINDS, evidence=["r2", "r3"]),
    ]


class Researcher(ls.EvidenceLlm):
    """The scripted author, whose endpoint (or step) may name its evidence by the path it read."""

    def __init__(self, queue: list[LlmResponse]) -> None:
        super().__init__(queue=queue, discovery_queue=planner())

    async def complete(self, request: LlmRequest) -> LlmResponse:
        response = await super().complete(request)
        if request.purpose != "learn":
            return response
        body = json.loads(response.content)
        text = "\n".join(message.content for message in request.messages)
        refs = {
            line.split()[2].split("?")[0]: line.rsplit("evidenceRef=", 1)[1]
            for line in text.splitlines()
            if line.startswith("[request] GET ") and " -> 2" in line
        }
        for item in [*body.get("endpoints", []), *body.get("bootstrap", [])]:
            spec = item.get("request", item)
            if spec.get("path", "").replace("{accountId}", "me") in refs:
                item["requestEvidenceId"] = refs[spec["path"].replace("{accountId}", "me")]
        return response.model_copy(update={"content": json.dumps(body, ensure_ascii=False)})


async def connect(
    make_env: EnvFactory,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    queue: list[LlmResponse],
) -> Any:
    monkeypatch.setattr(login, "ROOT_TARGET", "/learn")  # the cabinet is the whole site
    env = await make_env([])
    await env.login()
    env.services.browser = await scripted_lms_site(app)
    env.services.llm = Researcher(queue)
    return env


async def events(env: Any) -> list[m.ScheduleEvent]:
    env.db.expire_all()
    rows = await env.db.scalars(
        select(m.ScheduleEvent)
        .where(m.ScheduleEvent.connection_id == env.seeded.connection_id)
        .order_by(m.ScheduleEvent.starts_at, m.ScheduleEvent.title)
    )
    return list(rows)


def moscow(row: m.ScheduleEvent) -> tuple[str, str]:
    local = row.starts_at + timedelta(hours=3)  # Europe/Moscow has no DST
    return local.date().isoformat(), local.strftime("%H:%M")


def mondays(first: date, last: date, step: int = 7) -> list[str]:
    return [
        (first + timedelta(days=i)).isoformat() for i in range(0, (last - first).days + 1, step)
    ]


# --- the whole path -------------------------------------------------------------------------


async def test_rules_become_dated_classes_and_tasks_keep_every_status(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [author(schedule_endpoint()), author(deadlines_endpoint()), author(courses_endpoint())],
    )
    result = await env.run(payload={"url": ENTRY})
    assert result["status"] == "ok", result
    assert sorted(result["kinds"]) == ["courses", "deadlines", "scheduleEvents"]

    # 1. the recipe: v2, the rules unrolled by `weekly`, the repeating key as a list, no references
    stored = await env.spec(1)
    recipe = stored["recipe"]
    assert recipe["version"] == 2
    by_kind = {e["kind"]: e for e in recipe["endpoints"]}
    expansion = by_kind["scheduleEvents"]["response"]["expand"]
    assert expansion["type"] == "weekly"
    assert expansion["intervalWeeks"] == {"from": "row", "path": "/calendarEvent/schedule/interval"}
    assert by_kind["deadlines"]["request"]["query"] == {"state": list(lms.STATES)}
    assert "requestEvidenceId" not in json.dumps(stored)
    assert "bootstrap" not in recipe  # `me` is a word of the address, not a value to fetch

    # 2. the stand was asked with the whole list of states, every time
    assert lms.STATE.state_reads
    assert all(asked == list(lms.STATES) for asked in lms.STATE.state_reads)
    assert set(lms.STATE.segments) == {"me"}  # no id of the account ever replaced `me`

    # 3. the calendar: 5 rules over the weeks from Monday 26.10 to the end of the term 20.12
    rows = await events(env)
    got = {(row.title, *moscow(row)) for row in rows}
    first, last = date(2026, 10, 26), date(2026, 12, 20)

    def expect(title: str, weekday: int, clock: str, dates: list[str] | None = None) -> set[Any]:
        start = first + timedelta(days=weekday)
        days = dates or mondays(start, last)
        return {(title, day, clock) for day in days}

    expected = (
        expect("Линейная алгебра", 0, "09:30")
        | expect("Линейная алгебра", 2, "11:10")
        | expect("Основы баз данных", 1, "13:00")
        | expect(
            "Основы баз данных",
            4,
            "15:10",
            ["2026-10-30", "2026-11-13", "2026-11-27", "2026-12-11"],
        )
        | expect("Алгоритмы и структуры данных", 3, "09:30")
    )
    assert got == expected
    assert len(rows) == 36 == len(expected)
    assert len({row.external_id for row in rows}) == 36  # one stable id per rule and day

    # 4. one card: the room, the building, the teacher by name and nothing of the e-mail
    lecture = next(r for r in rows if moscow(r) == ("2026-10-26", "09:30"))
    assert (lecture.kind, lecture.format, lecture.location_text) == ("lecture", "offline", "N321")
    assert lecture.teacher_text == "Анна Королёва"
    assert lecture.ends_at - lecture.starts_at == timedelta(minutes=80)
    assert lecture.attributes == [{"label": "Корпус", "value": "ЦТ"}]
    online = next(r for r in rows if r.title == "Основы баз данных" and r.kind == "lecture")
    assert (online.format, online.location_text) == ("online", None)
    assert "@" not in json.dumps([r.teacher_text for r in rows], ensure_ascii=False)
    seminar = next(r for r in rows if r.kind == "seminar" and r.title == "Основы баз данных")
    assert seminar.attributes == [{"label": "Корпус", "value": "ГК"}]

    # 5. the tasks: every status is read, the words of the source are understood, the course known
    listed = await repo.list_deadlines(env.db, env.seeded.user_id, include_done=True, now=NOW)
    by_title = {d.title: d for d in listed}
    assert {t: d.state for t, d in by_title.items()} == {
        "Домашнее задание 4": "open",
        "Лабораторная работа 2": "open",
        "Контрольная 1": "submitted",
        "Домашнее задание 3": "missed",
        "Домашнее задание 2": "graded",
    }
    assert by_title["Домашнее задание 4"].course_title == "Линейная алгебра"
    assert by_title["Лабораторная работа 2"].course_title == "Основы баз данных"
    assert by_title["Домашнее задание 4"].due_at is not None
    assert by_title["Домашнее задание 4"].due_at.isoformat() == "2026-11-02T20:59:00+00:00"
    pending = await repo.list_deadlines(env.db, env.seeded.user_id, now=NOW)
    assert {d.title for d in pending} == {
        "Домашнее задание 4",
        "Лабораторная работа 2",
        "Домашнее задание 3",
    }
    assert await env.count(m.Course) == 3

    # 6. the model is not asked again
    asked = len(env.services.llm.requests)
    env.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    assert (await env.sync())["status"] == "ok"
    assert len(env.services.llm.requests) == asked
    assert len(await events(env)) == 36


# --- the author's mistakes of the incident, each told back in words --------------------------


def feedback_of(request: LlmRequest) -> str:
    """The remarks the author is given on a second attempt (a section of the task prompt)."""
    text = request.messages[1].content
    start = text.index("### Замечания к предыдущему варианту")
    return text[start : text.index("### Проверенные ссылки фабрики", start)]


async def test_a_dropped_repeating_key_is_told_back_with_its_count_and_then_read_whole(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [
            author(deadlines_endpoint("backlog")),  # one value of five, as the incident did
            author(deadlines_endpoint()),
        ],
    )
    result = await study.run(payload={"url": ENTRY, "kinds": ["deadlines"]})
    assert result["status"] == "ok", result

    remarks = feedback_of(study.services.llm.requests[1])
    assert "evidence_query_mismatch" in remarks
    assert "state×5" in remarks
    assert "state×1" in remarks
    assert "списком всех значений" in remarks
    assert "backlog" not in remarks  # keys and counts, never the values of the source
    stored = await study.spec(1)
    assert stored["recipe"]["endpoints"][0]["request"]["query"] == {"state": list(lms.STATES)}
    assert await study.count(m.Deadline) == 5


async def test_a_value_from_the_account_never_replaces_me_in_the_path(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = {
        "id": "account",
        "method": "GET",
        "path": lms.ME_PATH,
        "extract": {"accountId": "/id"},
    }
    guessed = schedule_endpoint()
    guessed["request"]["path"] = "/api/lms/students/{accountId}/timetables"
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [author(guessed, bootstrap=[account]), author(schedule_endpoint())],
    )
    result = await study.run(payload={"url": ENTRY, "kinds": ["scheduleEvents"]})
    assert result["status"] == "ok", result

    remarks = feedback_of(study.services.llm.requests[1])
    assert "evidence_path_literal_replaced" in remarks
    assert "`me`" in remarks
    assert "без bootstrap" in remarks
    assert "№4" in remarks  # the place, not a value
    # the wrong address was refused before it could ask the cabinet anything
    assert set(lms.STATE.segments) == {"me"}
    stored = await study.spec(1)
    assert stored["recipe"]["endpoints"][0]["request"]["path"] == lms.TIMETABLES_PATH
    assert "bootstrap" not in stored["recipe"]


# --- the source changes its rules: the next sync follows ------------------------------------


async def test_a_dropped_rule_takes_its_future_classes_off_and_a_new_one_adds_its_own(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [author(schedule_endpoint()), author(courses_endpoint())],
    )
    connected = await study.run(payload={"url": ENTRY, "kinds": ["scheduleEvents", "courses"]})
    assert connected["status"] == "ok"
    before = {(r.title, *moscow(r)) for r in await events(study)}
    assert len(before) == 36
    today = study.services.clock.now().date().isoformat()

    # the Monday lecture is dropped, a Wednesday seminar is added to the algorithms course, the
    # Thursday lecture moves by half an hour, the biweekly seminar becomes weekly
    lms.remove_rule(MONDAY_LECTURE)
    lms.add_rule(
        4103,
        lms.new_rule(
            "0b6c2f10-6f0e-4c3a-9a51-1f4d3e9b7a09",
            kind="seminar",
            room="N105",
            building="ГК",
            host="Игорь Миронов",
            day="wednesday",
            start="16:50",
            end="18:10",
            first="2026-10-28",
            last="2026-11-18",
        ),
    )
    for course in lms.STATE.timetables:
        for row in course["eventRows"]:
            schedule = row["calendarEvent"]["schedule"]
            if row["calendarEvent"]["calendarEventId"] == THURSDAY_LECTURE:
                schedule["startTime"] = "10:00"
            if row["calendarEvent"]["calendarEventId"] == BIWEEKLY_SEMINAR:
                schedule["interval"] = 1
    study.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    synced = await study.sync()
    assert synced["status"] == "ok"
    assert synced["kinds"]["scheduleEvents"]["missing"] > 0

    after = {(r.title, *moscow(r)) for r in await events(study)}
    lecture = {x for x in before if x[0] == "Линейная алгебра" and x[2] == "09:30"}
    assert len(lecture) == 8
    # the class that has begun stays as it happened, the future ones of the dropped rule are gone
    assert {x for x in after if x[0] == "Линейная алгебра" and x[2] == "09:30"} == {
        ("Линейная алгебра", "2026-10-26", "09:30")
    }
    algorithms = "Алгоритмы и структуры данных"
    old_time = {x for x in after if x[0] == algorithms and x[2] == "09:30"}
    assert not [x for x in old_time if x[1] >= today]  # the old time is gone ahead
    assert {x[1] for x in after if x[0] == algorithms and x[2] == "10:00"} == set(
        mondays(date(2026, 10, 29), date(2026, 12, 20))
    )
    added = {x[1] for x in after if x[0] == algorithms and x[2] == "16:50"}
    assert added == {"2026-10-28", "2026-11-04", "2026-11-11", "2026-11-18"}
    weekly_now = {x[1] for x in after if x[0] == "Основы баз данных" and x[2] == "15:10"}
    assert weekly_now == set(mondays(date(2026, 10, 30), date(2026, 12, 20)))

    # a second sync of the same answer changes nothing
    again = await study.sync()
    assert again["kinds"]["scheduleEvents"]["inserted"] == 0
    assert {(r.title, *moscow(r)) for r in await events(study)} == after
