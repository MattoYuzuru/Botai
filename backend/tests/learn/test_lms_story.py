"""A cabinet whose "my classes" page shows RULES: research, canary, sync and the domain (L1).

The synthetic SPA (`tests/fakeuni/lms.py`) answers the rules of every course
(`/api/lms/students/me/timetables`, a day of the week as a word) and the tasks behind a query whose
key repeats (`?state=a&state=b...`). The author's answers below are the ones the real model gave
on this stand (L2, `test_lms_live.py`), minus what is not data: no secret, no student value.
Nothing here reaches a real university or a real model.
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from botai.contracts.agent_ui import ConfirmBlock, InputBlock
from botai.core.dialogue import DialogueTurn
from botai.core.jobs import InMemoryJobContext, JobKind
from botai.core.llm import LlmRequest, LlmResponse
from botai.db import models as m
from botai.domain import repo
from botai.learn import revisions
from botai.learn.intent import LearningIntent
from botai.learn.progress import ActivationReceipt, intent_key
from botai.learn.progress import digest as research_digest
from botai.sources import calendar
from botai.sources.scope import covered_kinds
from tests.fakeuni import data, lms, login
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.learn.lms_env import ENTRY, scripted_lms_site
from tests.learn.test_conversation import ConversationContext
from tests.learn.test_probe import plan
from tests.sources.helpers import NOW
from tests.sources.support import seed_connection

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

    def __init__(
        self, queue: list[LlmResponse], discovery: list[LlmResponse] | None = None
    ) -> None:
        super().__init__(queue=queue, discovery_queue=planner() if discovery is None else discovery)

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
    discovery: list[LlmResponse] | None = None,
) -> Any:
    monkeypatch.setattr(login, "ROOT_TARGET", "/learn")  # the cabinet is the whole site
    env = await make_env([])
    await env.login()
    env.services.browser = await scripted_lms_site(app)
    env.services.llm = Researcher(queue, discovery)
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
        "Домашнее задание 3": "graded",  # handed in and marked 2 of 10: checked, not overdue
        "Домашнее задание 2": "graded",
    }
    assert by_title["Домашнее задание 4"].course_title == "Линейная алгебра"
    assert by_title["Лабораторная работа 2"].course_title == "Основы баз данных"
    assert by_title["Домашнее задание 4"].due_at is not None
    assert by_title["Домашнее задание 4"].due_at.isoformat() == "2026-11-02T20:59:00+00:00"
    pending = await repo.list_deadlines(env.db, env.seeded.user_id, now=NOW)
    assert {d.title for d in pending} == {"Домашнее задание 4", "Лабораторная работа 2"}
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


# --- what the student is told when something is left ----------------------------------------


def said(study: Any) -> str:
    return "\n".join(block.markdown for block in ls.blocks_of(study.ctx, "text"))


async def test_a_kind_that_cannot_be_connected_is_explained_not_coded(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [
            author(schedule_endpoint()),
            author(deadlines_endpoint("backlog")),  # the author is told, and repeats himself
            author(deadlines_endpoint("backlog")),
            author(courses_endpoint()),
        ],
    )
    # the student asked for all three: what was not read is asked about, not just noted
    result = await study.run(payload={"url": ENTRY, "kinds": KINDS}, choices=["skip"])
    assert result["status"] == "ok", result
    assert sorted(result["kinds"]) == ["courses", "scheduleEvents"]

    text = said(study)
    # what is left, why (in words, from the check that refused it) and what can be done
    assert "Подключаю не всё. Не вышло: дедлайны — описание запроса не совпало с тем, что" in text
    assert "отправляла сама страница (параметры адреса)" in text
    assert "Можно прислать ссылку на страницу, где это видно." in text
    asked = [b for b in study.asked if hasattr(b, "question")]
    assert asked[0].question.startswith("Не вышло: дедлайны — описание запроса не совпало")
    for code in ("evidence_", "lint", "canary", "requestEvidenceId", "bootstrap"):
        assert code not in text
    assert "backlog" not in text  # no value of the source is repeated to the student


# --- what the student is asked, and what a link to another site does to what was accepted -------

OTHER_SITE = "https://other.fakeuni.test/week"


def locations_endpoint() -> dict[str, Any]:
    """Rooms nobody asked for. The rule has no field with a room, so the check refuses them."""
    row = {"from": "row"}
    return {
        "id": "locations",
        "kind": "locations",
        "request": {"method": "GET", "path": lms.TIMETABLES_PATH},
        "response": {
            "format": "json",
            "collections": [{"as": "course", "path": ""}, {"as": "row", "path": "/eventRows"}],
            "fields": {
                "externalId": {**row, "path": "/calendarEvent/calendarEventId", "required": True},
                "title": {**row, "path": "/calendarEvent/roomTitle", "required": True},
            },
        },
    }


def finish_with(*kinds: str) -> list[LlmResponse]:
    """The planner opens the timetable and hands over, having seen `kinds` in the cabinet."""
    return [
        plan(1, "navigate", "l0", kinds=["scheduleEvents"], evidence=["p0", "r2"]),
        plan(2, "finish", kinds=list(kinds), evidence=["r2", "r3"]),
    ]


async def test_a_kind_the_research_tried_on_its_own_never_asks_the_student_anything(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident: the schedule, the courses were accepted, rooms nobody asked for were refused,
    and the student was asked for a link to a page with rooms."""
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [
            author(schedule_endpoint()),
            author(courses_endpoint()),
            author(locations_endpoint()),
            author(locations_endpoint()),
        ],
        discovery=finish_with("scheduleEvents", "courses", "locations"),
    )
    result = await study.run(payload={"url": ENTRY})  # the student named no kind: auto mode
    assert result["status"] == "ok", result
    assert sorted(result["kinds"]) == ["courses", "scheduleEvents"]
    assert (await study.conn()).status == "active"
    assert await study.count(m.ScheduleEvent) > 0

    # straight to the summary: nothing to choose, no link to give
    assert [type(b).__name__ for b in study.asked] == ["ConfirmBlock"]
    text = said(study)
    assert "Подключаю не всё" not in text
    assert "аудитор" not in text.lower().replace("заодно", "")  # not as a question or a promise
    confirm = study.asked[0]
    assert (
        "Заодно пробовала, но не вышло (это не мешает остальному): аудитории — " in confirm.details
    )
    assert "- **Расписание:**" in confirm.details
    assert "- **Курсы:**" in confirm.details
    for code in ("evidence_", "required_fields_missing", "canary"):
        assert code not in confirm.details


async def test_when_everything_asked_for_is_accepted_the_summary_comes_without_questions(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [author(schedule_endpoint()), author(deadlines_endpoint())],
        discovery=finish_with("scheduleEvents", "deadlines", "courses", "locations"),
    )
    result = await study.run(payload={"url": ENTRY, "kinds": ["scheduleEvents", "deadlines"]})
    assert result["status"] == "ok", result
    assert sorted(result["kinds"]) == ["deadlines", "scheduleEvents"]  # nothing is tried unasked
    assert [type(b).__name__ for b in study.asked] == ["ConfirmBlock"]
    assert "Заодно" not in study.asked[0].details


async def test_a_requested_kind_that_failed_is_still_asked_about(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [
            author(schedule_endpoint()),
            author(deadlines_endpoint("backlog")),
            author(deadlines_endpoint("backlog")),
        ],
    )
    result = await study.run(
        payload={"url": ENTRY, "kinds": ["scheduleEvents", "deadlines"]}, choices=["skip"]
    )
    assert result["status"] == "ok", result
    assert [type(b).__name__ for b in study.asked] == ["ChoiceBlock", "ConfirmBlock"]
    assert study.asked[0].question.startswith("Не вышло: дедлайны — ")
    assert "Подключаю не всё. Не вышло: дедлайны" in said(study)


async def test_a_link_to_another_site_saves_what_was_accepted_first(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident: the student answers the question about the missing tasks with a link to
    another site, and the accepted classes used to be thrown away with the connection left a draft."""
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [
            author(schedule_endpoint()),
            author(deadlines_endpoint("backlog")),
            author(deadlines_endpoint("backlog")),
        ],
    )
    seen: list[dict[str, Any]] = []

    async def continue_source(ctx: Any, services: Any, parent: str, url: str, intent: Any) -> Any:
        """The sibling starts when the classes are already stored and the source is active."""
        seen.append(
            {
                "parent": parent,
                "url": url,
                "intent": intent,
                "specs": await study.specs(),
                "events": len(await events(study)),
                "status": (await study.conn()).status,
                "told": said(study),
            }
        )
        return {"connectionId": "sibling-fixture", "jobId": "sibling-job-fixture"}

    monkeypatch.setattr("botai.spaces.conversations.continue_source", continue_source)

    result = await study.run(
        payload={"url": ENTRY, "kinds": ["scheduleEvents", "deadlines"]},
        choices=["link"],
        links=[OTHER_SITE],
    )

    assert result["status"] == "ok", result
    assert result["kinds"] == ["scheduleEvents"]
    assert result["continuation"] == {
        "connectionId": "sibling-fixture",
        "jobId": "sibling-job-fixture",
    }
    (started,) = seen
    assert started["parent"] == study.seeded.connection_id
    assert started["url"] == OTHER_SITE
    assert started["specs"] == [(1, "active")]  # saved before the sibling began
    assert started["events"] > 0
    assert started["status"] == "active"
    assert "Готово!" in started["told"]  # the closing line of the saved connection came first
    # the sibling is asked for what this connection did not get, from the new page only
    assert started["intent"]["entry_urls"] == [OTHER_SITE]
    assert started["intent"]["requested_kinds"] == ["deadlines"]
    assert LearningIntent.restore(started["intent"]).discovery_mode == "requested"

    conn = await study.conn()
    assert (conn.status, conn.adapter) == ("active", "recipe")
    assert conn.spec_id is not None
    assert await study.specs() == [(1, "active")]
    assert [type(b).__name__ for b in study.asked] == ["ChoiceBlock", "InputBlock", "ConfirmBlock"]
    text = said(study)
    assert "Подключаю не всё. Не вышло: дедлайны" in text
    assert text.index("Готово!") < text.index("Эта страница находится на другом сайте")


async def test_declining_the_summary_still_takes_the_link_and_leaves_the_status_alone(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [
            author(schedule_endpoint()),
            author(deadlines_endpoint("backlog")),
            author(deadlines_endpoint("backlog")),
        ],
    )
    taken: list[str] = []

    async def continue_source(ctx: Any, services: Any, parent: str, url: str, intent: Any) -> Any:
        taken.append(url)
        return {"connectionId": "sibling-fixture", "jobId": "sibling-job-fixture"}

    monkeypatch.setattr("botai.spaces.conversations.continue_source", continue_source)

    result = await study.run(
        payload={"url": ENTRY, "kinds": ["scheduleEvents", "deadlines"]},
        choices=["link"],
        links=[OTHER_SITE],
        confirm=False,
    )

    assert taken == [OTHER_SITE]  # the link was the student's own ask
    assert result["status"] == "error"  # nothing was saved, and the status says so
    assert "continuation" in result
    conn = await study.conn()
    assert (conn.status, conn.spec_id) == ("error", None)
    assert await study.specs() == []
    assert await study.count(m.ScheduleEvent) == 0


async def test_a_link_given_while_the_summary_waits_does_not_make_the_summary_stale(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link to another site is not a change of what is being connected here: the summary the
    student confirmed is saved as it is, and the link goes on afterwards."""
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [author(schedule_endpoint()), author(deadlines_endpoint())],
    )
    taken: list[dict[str, Any]] = []

    async def continue_source(ctx: Any, services: Any, parent: str, url: str, intent: Any) -> Any:
        taken.append({"url": url, "specs": await study.specs()})
        return {"connectionId": "sibling-fixture", "jobId": "sibling-job-fixture"}

    monkeypatch.setattr("botai.spaces.conversations.continue_source", continue_source)
    ctx = ConversationContext(
        user_id=study.seeded.user_id, connection_id=study.seeded.connection_id
    )

    def respond(block: Any) -> Any:
        if isinstance(block, ConfirmBlock):  # the student answers, and adds a link from the phone
            ctx.turns.append(DialogueTurn("turn-link-fixture", OTHER_SITE, NOW))
            return True
        return None

    ctx._responder = respond
    payload = {"url": ENTRY, "kinds": ["scheduleEvents", "deadlines"]}
    result = await study.handlers[JobKind.CONNECT](ctx, study.services, payload)

    assert result["status"] == "ok", result
    assert sorted(result["kinds"]) == ["deadlines", "scheduleEvents"]
    assert [b for b in ctx.blocks if isinstance(b, ConfirmBlock)].__len__() == 1  # asked once
    assert taken == [{"url": OTHER_SITE, "specs": [(1, "active")]}]
    assert (await study.conn()).status == "active"


# --- a calendar service is no cabinet: its page is not a source, a calendar has its own way ------

CALENDAR_PAGE = "https://calendar.yandex.ru/week?uid=123"


async def forbid_siblings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    taken: list[str] = []

    async def continue_source(ctx: Any, services: Any, parent: str, url: str, intent: Any) -> Any:
        taken.append(url)
        raise AssertionError("a page of a calendar service must not become a source")

    monkeypatch.setattr("botai.spaces.conversations.continue_source", continue_source)
    return taken


async def test_a_link_to_a_calendar_service_is_no_source_and_the_accepted_kinds_are_kept(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident: the student answered with a link to a web calendar, the second source went to
    log in to Yandex ID, and the accepted classes were lost."""
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [
            author(schedule_endpoint()),
            author(deadlines_endpoint("backlog")),
            author(deadlines_endpoint("backlog")),
        ],
    )
    taken = await forbid_siblings(monkeypatch)

    result = await study.run(
        payload={"url": ENTRY, "kinds": ["scheduleEvents", "deadlines"]},
        choices=["link"],
        links=[CALENDAR_PAGE],
    )

    assert taken == []
    assert result["status"] == "ok", result
    assert result["kinds"] == ["scheduleEvents"]
    assert "continuation" not in result
    conn = await study.conn()
    assert (conn.status, conn.adapter) == ("active", "recipe")
    assert await study.count(m.ScheduleEvent) > 0
    assert [type(b).__name__ for b in study.asked] == ["ChoiceBlock", "InputBlock", "ConfirmBlock"]

    # the student is told, in words, that a calendar is not a cabinet and how its link is taken
    text = said(study)
    assert "Это страница календаря в браузере, а не кабинет вуза" in text
    assert "«Экспорт»" in text
    assert "Секретный адрес в формате iCal" in text
    assert "Эта страница находится на другом сайте" not in text
    assert text.count("Это страница календаря в браузере") == 1
    assert text.index("Это страница календаря") < text.index("Подключаю не всё")
    assert "calendar.yandex.ru" not in text  # the address of the page is not repeated


async def test_a_calendar_service_given_at_the_start_is_not_taken_for_the_site_to_connect(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [author(schedule_endpoint()), author(deadlines_endpoint())],
    )
    taken = await forbid_siblings(monkeypatch)
    intent = {
        "entry_urls": [CALENDAR_PAGE, ENTRY],
        "requested_kinds": ["scheduleEvents", "deadlines"],
        "absent_kinds": [],
        "user_note": "",
    }

    result = await study.run(payload={"learning_intent": intent})

    assert taken == []
    assert result["status"] == "ok", result
    assert sorted(result["kinds"]) == ["deadlines", "scheduleEvents"]
    assert said(study).count("Это страница календаря в браузере") == 1


async def test_a_source_that_is_a_calendar_service_is_not_learned_through_its_login(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling made of such a page before this was fixed: it must not open a browser login."""
    study = await connect(make_env, fakeuni_app, monkeypatch, [author(schedule_endpoint())])
    conn = await study.conn()
    conn.origin, conn.allow_hosts = "https://calendar.yandex.ru", ["calendar.yandex.ru"]
    await study.db.flush()

    result = await study.run(payload={"url": "https://calendar.yandex.ru/week"})

    assert result == {"status": "skipped", "detail": "calendar"}
    assert study.services.llm.requests == []  # nothing was observed, no model was asked
    assert study.asked == []
    conn = await study.conn()
    assert (conn.status, conn.spec_id) == ("draft", None)  # as it was: only the offer is made
    text = said(study)
    assert "Это страница календаря в браузере" in text
    assert "Не получилось" not in text


async def test_a_calendar_service_is_not_accepted_as_the_site_to_connect(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source without an address asks for the site; a calendar page is an answer that gets the
    way to a calendar, not a second question and not a source."""
    study = await connect(make_env, fakeuni_app, monkeypatch, [author(schedule_endpoint())])
    conn = await study.conn()
    conn.origin, conn.allow_hosts = "", []
    await study.db.flush()

    result = await study.run(payload={}, links=[CALENDAR_PAGE, ENTRY])

    assert result == {"status": "skipped", "detail": "calendar"}
    assert [type(b).__name__ for b in study.asked] == ["InputBlock"]  # asked once, not twice
    assert study.services.llm.requests == []
    text = said(study)
    assert "Это страница календаря в браузере" in text
    assert "Не получилось" not in text  # the offer is all there is to say
    conn = await study.conn()
    assert (conn.origin, conn.status) == ("", "draft")  # nothing written, the status as it was


async def test_the_calendar_question_goes_into_the_thread_of_the_task_with_its_buttons(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [
            author(schedule_endpoint()),
            author(deadlines_endpoint("backlog")),
            author(deadlines_endpoint("backlog")),
        ],
    )
    # the task lives in a thread of the university, as a task started by the agent does
    conn = await study.conn()
    thread = m.ChatThread(user_id=study.seeded.user_id, space_id=conn.space_id, title="Подключение")
    study.db.add(thread)
    await study.db.flush()
    thread_id = thread.id
    study.db.add(
        m.Job(
            id="01J00000000000000000000JOB",
            user_id=study.seeded.user_id,
            kind="connect",
            state="running",
            connection_id=study.seeded.connection_id,
            thread_id=thread_id,
            payload={},
            run_after=NOW,
        )
    )
    await study.db.flush()

    result = await study.run(
        payload={"url": ENTRY, "kinds": ["scheduleEvents", "deadlines"]},
        choices=["link"],
        links=[CALENDAR_PAGE],
    )

    assert result["status"] == "ok", result
    study.db.expire_all()
    questions = list(
        await study.db.scalars(
            select(m.ChatMessage).where(
                m.ChatMessage.thread_id == thread_id, m.ChatMessage.role == "agent"
            )
        )
    )
    (question,) = [q.blocks[0] for q in questions if "Подключить календарь" in str(q.blocks[0])]
    assert question["markdown"].startswith(calendar.PAGE_LEAD)
    assert "Подключить календарь вуза «Фейкуни»?" in question["markdown"]
    assert "«Экспорт»" in question["markdown"]  # how the export link is taken
    assert question["quickReplies"] == [calendar.YES, calendar.NO]
    assert "calendar.yandex.ru" not in str(question)
    assert "Это страница календаря в браузере" not in said(study)  # said once, where it belongs


# --- a source that works is extended: only the missing kinds are learned ---------------------------


class Watching(InMemoryJobContext):
    """A task whose questions are answered like the usual ones, and which notes what the source
    looked like at the moment each was asked."""

    def __init__(self, study: Any, on_confirm: Any = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.study, self.seen, self.on_confirm = study, [], on_confirm

    async def ask(self, block: Any) -> Any:
        self.seen.append(
            (
                type(block).__name__,
                (await self.study.conn()).status,
                len(await events(self.study)),
                await self.study.specs(),
            )
        )
        if self.on_confirm is not None and isinstance(block, ConfirmBlock):
            await self.on_confirm()  # something happens while the student reads the summary
        return await super().ask(block)


def classified(**kw: Any) -> LlmResponse:
    body = {"linkIndices": [], "kinds": [], "absentKinds": [], "note": "Добавь задания", **kw}
    return LlmResponse(content=json.dumps(body, ensure_ascii=False))


async def working_schedule_only(
    make_env: EnvFactory, app: FastAPI, monkeypatch: pytest.MonkeyPatch, *answers: LlmResponse
) -> tuple[Any, set[tuple[str, str, str, str]]]:
    """A cabinet whose schedule is connected and read: the recipe has no tasks."""
    study = await connect(make_env, app, monkeypatch, [author(schedule_endpoint())])
    first = await study.run(payload={"url": ENTRY, "kinds": ["scheduleEvents"]})
    assert first["status"] == "ok", first
    assert await study.specs() == [(1, "active")]
    assert (await study.conn()).data_scope == ["scheduleEvents"]
    assert await study.count(m.Deadline) == 0
    rows = {(r.external_id, r.title, *moscow(r)) for r in await events(study)}
    assert rows
    llm = study.services.llm
    llm._queue.extend(answers)
    llm._discovery_queue[:] = finish_with("deadlines")
    return study, rows


EXTENSION = {
    "entry_urls": [ENTRY],
    "requested_kinds": ["deadlines"],
    "absent_kinds": [],
    "user_note": "Добавь задания",
    "discovery_mode": "requested",
}


async def extend(
    study: Any,
    *,
    confirm: bool = True,
    on_confirm: Any = None,
    note_applied: bool = False,
    **intent: Any,
) -> tuple[Watching, Any]:

    def respond(block: Any) -> Any:
        if isinstance(block, ConfirmBlock):
            return confirm
        if isinstance(block, InputBlock):
            return " "  # the student has no link to give
        return None

    ctx = Watching(
        study,
        user_id=study.seeded.user_id,
        connection_id=study.seeded.connection_id,
        responder=respond,
        on_confirm=on_confirm,
    )
    study.ctx = ctx  # what `said` reads
    payload: dict[str, Any] = {
        "learning_intent": {**EXTENSION, **intent},
        "extend": True,
        "source": "chat",
    }
    if note_applied:
        payload["note_applied"] = True
    result = await study.handlers[JobKind.CONNECT](ctx, study.services, payload)
    return ctx, result


async def test_tasks_added_to_a_working_schedule_are_learned_alone_and_the_schedule_stays(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study, before = await working_schedule_only(
        make_env,
        fakeuni_app,
        monkeypatch,
        classified(kinds=["deadlines"]),
        author(deadlines_endpoint()),
    )
    schedule_before = (await study.spec(1))["recipe"]["endpoints"]

    ctx, result = await extend(study)

    assert result["status"] == "ok", result
    assert result["kinds"] == ["deadlines"]  # what was added, not what was already there
    assert "extension" not in result
    # the question was asked while the source still worked as it had: active, the schedule stored
    (asked,) = ctx.seen
    assert asked == ("ConfirmBlock", "active", len(before), [(1, "active")])
    assert study.services.llm._queue == []  # one author call, for the tasks alone

    # the recipe has both; the schedule endpoint is the very one that was there
    specs = await study.specs()
    assert len(specs) == 2
    assert specs[-1] == (2, "active")
    endpoints = (await study.spec(2))["recipe"]["endpoints"]
    assert {e["kind"] for e in endpoints} == {"scheduleEvents", "deadlines"}
    assert [e for e in endpoints if e["kind"] == "scheduleEvents"] == schedule_before
    conn = await study.conn()
    assert (conn.status, conn.spec_revision) == ("active", 2)
    assert conn.data_scope == ["scheduleEvents", "deadlines"]
    assert {(r.external_id, r.title, *moscow(r)) for r in await events(study)} == before
    assert await study.count(m.Deadline) > 0
    text = said(study)
    assert "Готово!" in text  # the closing line counts both
    assert "Не вышло" not in text

    # and both go on being read: a sync after the extension keeps the schedule and the tasks
    synced = await study.sync()
    assert synced["status"] == "ok", synced
    assert {(r.external_id, r.title, *moscow(r)) for r in await events(study)} == before
    assert await study.count(m.Deadline) > 0


async def test_an_extension_that_finds_nothing_leaves_the_working_source_as_it_was(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study, before = await working_schedule_only(
        make_env,
        fakeuni_app,
        monkeypatch,
        classified(kinds=["deadlines"]),
        author(deadlines_endpoint("backlog")),
        author(deadlines_endpoint("backlog")),
    )
    _, result = await extend(study)

    assert result["status"] == "error"
    assert result["extension"] is True
    conn = await study.conn()
    assert (conn.status, conn.spec_revision, conn.error_code) == ("active", 1, None)
    assert conn.data_scope == ["scheduleEvents"]
    assert await study.specs() == [(1, "active")]
    assert {(r.external_id, r.title, *moscow(r)) for r in await events(study)} == before
    text = said(study)
    assert "Уже подключённые данные работают как раньше." in text
    assert "Не получилось подключить" not in text
    assert (await study.sync())["status"] == "ok"


async def test_declining_the_added_tasks_changes_nothing(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study, before = await working_schedule_only(
        make_env,
        fakeuni_app,
        monkeypatch,
        classified(kinds=["deadlines"]),
        author(deadlines_endpoint()),
    )
    _, result = await extend(study, confirm=False)
    assert result["status"] == "error"
    conn = await study.conn()
    assert (conn.status, conn.spec_revision, conn.data_scope) == ("active", 1, ["scheduleEvents"])
    assert await study.specs() == [(1, "active")]
    assert await study.count(m.Deadline) == 0
    assert {(r.external_id, r.title, *moscow(r)) for r in await events(study)} == before


async def test_a_clarification_cannot_take_the_schedule_off_a_source_while_adding_tasks(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study, before = await working_schedule_only(
        make_env,
        fakeuni_app,
        monkeypatch,
        classified(kinds=["deadlines"], absentKinds=["scheduleEvents"], replaceKinds=True),
        author(deadlines_endpoint()),
    )
    _, result = await extend(study)
    assert result["status"] == "ok", result
    conn = await study.conn()
    assert conn.data_scope == ["scheduleEvents", "deadlines"]  # neither narrowed nor replaced
    assert {e["kind"] for e in (await study.spec(2))["recipe"]["endpoints"]} == {
        "scheduleEvents",
        "deadlines",
    }
    assert {(r.external_id, r.title, *moscow(r)) for r in await events(study)} == before


async def test_kinds_a_source_reads_already_are_not_learned_again(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study, _ = await working_schedule_only(make_env, fakeuni_app, monkeypatch)
    requests_before = len(study.services.llm.requests)
    _, result = await extend(study, requested_kinds=["scheduleEvents"], user_note="")
    assert result == {"status": "ok", "kinds": [], "extension": True, "detail": "covered"}
    assert len(study.services.llm.requests) == requests_before  # no model, no browser
    assert "Эти данные уже подключены: расписание" in said(study)
    assert (await study.conn()).status == "active"
    assert await study.specs() == [(1, "active")]


# --- review round: the ask of now beats an old note; other students; a base that moved -------------


async def test_a_note_kept_from_the_dialogue_is_not_read_again_to_take_the_kinds_away(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """«только расписание» was said long ago; the classifier must not run on it again."""
    study, before = await working_schedule_only(
        make_env, fakeuni_app, monkeypatch, author(deadlines_endpoint())
    )
    _, result = await extend(study, user_note="только расписание", note_applied=True)
    assert result["status"] == "ok", result  # no classifier answer was scripted: none was asked
    assert result["kinds"] == ["deadlines"]
    assert (await study.conn()).data_scope == ["scheduleEvents", "deadlines"]
    assert {(r.external_id, r.title, *moscow(r)) for r in await events(study)} == before


async def test_an_exclusion_of_what_the_source_reads_is_dropped_when_the_extension_begins(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study, _ = await working_schedule_only(
        make_env, fakeuni_app, monkeypatch, author(deadlines_endpoint())
    )
    _, result = await extend(study, user_note="", absent_kinds=["scheduleEvents", "news"])
    assert result["status"] == "ok", result
    # the schedule is read: the old exclusion does not narrow the source; the other one stays
    assert (await study.conn()).data_scope == ["scheduleEvents", "deadlines"]


async def second_student(study: Any, scope: list[str] | None) -> str:
    """Another student of the same site, whose source was connected from the same recipe."""
    other = await seed_connection(
        study.db, spec=await study.spec(1), origin=data.ORIGIN, university=True
    )
    if scope is None:  # no scope at all: SQL NULL (a column never written) or a JSON null
        await study.db.execute(
            text("UPDATE connections SET data_scope = 'null'::jsonb WHERE id = :id"),
            {"id": other.connection_id},
        )
    else:
        conn = await study.db.get(m.Connection, other.connection_id)
        assert conn is not None
        conn.data_scope = scope
    await study.db.flush()
    return other.connection_id


async def test_an_extension_does_not_make_other_students_read_the_new_kind(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study, _ = await working_schedule_only(
        make_env, fakeuni_app, monkeypatch, author(deadlines_endpoint())
    )
    reads_all = await second_student(study, None)
    reads_schedule = await second_student(study, ["scheduleEvents"])

    _, result = await extend(study, user_note="")
    assert result["status"] == "ok", result

    async def scope_of(connection_id: str) -> list[str] | None:
        study.db.expire_all()
        conn = await study.db.get(m.Connection, connection_id)
        assert conn is not None
        return None if conn.data_scope is None else list(conn.data_scope)

    assert await scope_of(study.seeded.connection_id) == ["scheduleEvents", "deadlines"]
    # a student who read every kind of the recipe keeps reading what the recipe had: no tasks
    pinned = await scope_of(reads_all)
    assert pinned == ["scheduleEvents"]
    active = await study.spec(2)
    assert covered_kinds(active, None) == ("scheduleEvents", "deadlines")
    assert covered_kinds(active, tuple(pinned or ())) == ("scheduleEvents",)
    # one who had a scope of his own is not touched
    assert await scope_of(reads_schedule) == ["scheduleEvents"]


async def test_a_revision_that_adds_no_kind_leaves_other_students_scopes_alone(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [author(schedule_endpoint()), author(schedule_endpoint())],
    )
    first = await study.run(payload={"url": ENTRY, "kinds": ["scheduleEvents"]})
    assert first["status"] == "ok", first
    reads_all = await second_student(study, None)
    # the same kinds again, in a new revision (what a repair or a relearn does)
    llm = study.services.llm
    llm._discovery_queue[:] = finish_with("scheduleEvents")
    again = await study.run(payload={"url": ENTRY, "kinds": ["scheduleEvents"]})
    assert again["status"] == "ok", again
    assert len(await study.specs()) == 2
    study.db.expire_all()
    other = await study.db.get(m.Connection, reads_all)
    assert other is not None
    assert other.data_scope is None


async def test_a_repair_that_lands_while_the_summary_waits_is_not_taken_back(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The student reads «Всё верно?»; meanwhile a repair makes revision 2 the active one. The
    «Да» must save the tasks on top of the repaired recipe, not on the one it was built from."""
    study, before = await working_schedule_only(
        make_env, fakeuni_app, monkeypatch, author(deadlines_endpoint())
    )

    async def repair_lands() -> None:
        spec = copy.deepcopy(await study.spec(1))
        schedule = next(e for e in spec["recipe"]["endpoints"] if e["kind"] == "scheduleEvents")
        schedule["response"]["expand"]["horizon"]["days"] = 60  # what the repair changed
        async with study.services.db() as db, db.begin():
            row = await revisions.add_revision(
                db, origin=data.ORIGIN, adapter="recipe", spec=spec, state="canary"
            )
            await revisions.activate(db, row, now=NOW)

    _, result = await extend(study, user_note="", on_confirm=repair_lands)

    assert result["status"] == "ok", result
    assert result["kinds"] == ["deadlines"]
    specs = await study.specs()
    assert specs[-1] == (3, "active")  # the repair is 2, the tasks are built on it as 3
    endpoints = (await study.spec(3))["recipe"]["endpoints"]
    assert {e["kind"] for e in endpoints} == {"scheduleEvents", "deadlines"}
    schedule = next(e for e in endpoints if e["kind"] == "scheduleEvents")
    assert schedule["response"]["expand"]["horizon"]["days"] == 60  # the repair is kept
    conn = await study.conn()
    assert (conn.status, conn.spec_revision) == ("active", 3)
    assert {(r.external_id, r.title, *moscow(r)) for r in await events(study)} == before
    assert await study.count(m.Deadline) > 0


async def test_a_restart_after_the_recipe_was_saved_finishes_the_task_not_stops_at_covered(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study, _ = await working_schedule_only(
        make_env, fakeuni_app, monkeypatch, author(deadlines_endpoint())
    )
    _, saved = await extend(study, user_note="")
    assert saved["status"] == "ok", saved
    row = await revisions.active_row(study.db, data.ORIGIN, "recipe")
    assert row is not None
    assert row.revision == 2
    receipt = ActivationReceipt(
        intent_key=intent_key(LearningIntent.restore({**EXTENSION, "user_note": ""}), data.ORIGIN),
        spec_id=row.id,
        revision=2,
        spec_digest=research_digest(row.spec),
        kinds=["deadlines"],
        relearn=True,
    )
    # the job of the extension as the queue holds it after a restart: its receipt is there
    study.db.add(
        m.Job(
            id="01J00000000000000000000JOB",
            user_id=study.seeded.user_id,
            kind="connect",
            state="running",
            connection_id=study.seeded.connection_id,
            payload={"_research_activation": receipt.model_dump()},
            run_after=NOW,
        )
    )
    await study.db.flush()

    ctx, result = await extend(study, user_note="")

    assert result["status"] == "ok", result
    assert result["resumed_activation"] is True  # the first read of the new kinds was made now
    assert result["kinds"] == ["deadlines"]
    text = said(study)
    assert "Готово!" in text
    assert "уже подключены" not in text
    assert ctx.seen == []  # nothing was asked again


async def test_nothing_connected_says_what_was_seen_tried_and_can_be_done(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [author(deadlines_endpoint("backlog"))] * 8,
    )
    result = await study.run(
        payload={"url": ENTRY, "kinds": ["deadlines"]},
        links=[ENTRY.replace("timetable", "tasks")] * 2,
    )
    assert result["status"] == "error"
    text = said(study)
    assert "Ничего не подключила." in text
    assert "Что нашла: открывала «" in text
    assert "«Мои пары»" in text
    assert "«Задания»" in text
    assert "Не вышло: дедлайны — описание запроса не совпало с тем, что отправляла сама" in text
    assert (
        "Можно прислать ссылку на страницу, где это видно, или написать «подключи ещё раз»." in text
    )
    assert "Пока не удалось проверить" not in text
    assert "план просмотра не сработал" not in text
    question = next(b.question for b in study.asked if hasattr(b, "question"))
    assert question.startswith("Не вышло: дедлайны — ")
    assert "Пришлите ссылку на страницу, где это видно" in question


async def test_an_incomplete_look_says_why_it_stopped_and_what_it_saw(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [author(schedule_endpoint())],
        discovery=[LlmResponse(content="the planner lost the thread")],
    )
    result = await study.run(payload={"url": ENTRY, "kinds": ["scheduleEvents"]})
    assert result["status"] == "ok", result
    text = said(study)
    assert "Просмотрела кабинет не до конца: помощник выбрал следующий шаг неразборчиво" in text
    assert "Что нашла: открывала «Мои пары»" in text
    assert text.count("Это не значит, что данных нет.") == 1
    assert "план просмотра не сработал" not in text
    assert "исследование достигло ограничения" not in text


@pytest.mark.parametrize("breakage", ["every rule gone", "address not found"])
async def test_an_empty_or_failing_answer_takes_no_class_off(
    make_env: EnvFactory, fakeuni_app: FastAPI, monkeypatch: pytest.MonkeyPatch, breakage: str
) -> None:
    study = await connect(
        make_env,
        fakeuni_app,
        monkeypatch,
        [author(schedule_endpoint()), author(courses_endpoint())],
    )
    await study.run(payload={"url": ENTRY, "kinds": ["scheduleEvents", "courses"]})
    before = {(r.title, *moscow(r)) for r in await events(study)}
    assert len(before) == 36

    # every rule gone at once is a cabinet that broke, not a student with no classes; an address
    # that no longer answers is not an answer at all: the sync keeps the calendar and sends the
    # recipe to repair
    if breakage == "every rule gone":
        lms.STATE.timetables = []
    else:
        lms.STATE.timetables_status = 404
    study.services.clock.advance(timedelta(hours=3))  # type: ignore[attr-defined]
    assert (await study.sync())["status"] == "repairing"
    assert {(r.title, *moscow(r)) for r in await events(study)} == before
