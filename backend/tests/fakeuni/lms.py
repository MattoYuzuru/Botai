"""A synthetic single-page cabinet whose "my classes" page shows repeating RULES, not a calendar.

* `/learn/timetable` (SPA): reads the account (`/api/account/me`, the bootstrap with the student's
  id) and the rules of every course (`/api/lms/students/me/timetables`, cookie session, no bearer).
  A rule is `{startDate, endDate, dayOfWeek: "monday", startTime, endTime, interval}`; the calendar
  has to be unrolled from it. The path accepts only the literal `me`: the id from the account
  answers 404, like a real endpoint that has no resource for a foreign id.
* `/learn/tasks` (SPA): reads `/api/lms/tasks/student?state=...&state=...`, a query whose key
  REPEATS. A request that omits some of the states gets only the tasks in the given ones.

Names, rooms, people and ids are invented. Nothing here resembles a real university.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from tests.fakeuni.login import is_logged_in

router = APIRouter()

TIMETABLE_PAGE = "/learn/timetable"
TASKS_PAGE = "/learn/tasks"
ME_PATH = "/api/account/me"
TIMETABLES_PATH = "/api/lms/students/me/timetables"
TASKS_PATH = "/api/lms/tasks/student"
ACCOUNT_ID = "5b1d7a52-0c64-4f0e-9a6b-8e8a7a9f3c11"  # what `/api/account/me` calls the student
STATES = ("backlog", "inProgress", "review", "failed", "evaluated")


def _event(
    uid: str,
    kind: str,
    number: int,
    *,
    room: str | None,
    building: str | None,
    host: str,
    day: str,
    start: str,
    end: str,
    first: str,
    last: str,
    interval: int = 1,
    fmt: str = "offline",
    comment: str | None = None,
) -> dict[str, Any]:
    return {
        "eventType": kind,
        "eventRowNumber": number,
        "calendarEvent": {
            "calendarEventId": uid,
            "eventType": kind,
            "location": (
                {"id": 700 + number, "title": room, "building": building}
                if room is not None
                else None
            ),
            "hosts": [{"name": host, "email": f"{host.split()[0].lower()}@staff.fakeuni.test"}],
            "schedule": {
                "startDate": first,
                "endDate": last,
                "startTime": start,
                "endTime": end,
                "dayOfWeek": day,
                "interval": interval,
                "comment": comment,
            },
            "format": fmt,
        },
    }


def default_timetables() -> list[dict[str, Any]]:
    return [
        {
            "courseId": 4101,
            "courseName": "Линейная алгебра",
            "eventRows": [
                _event(
                    "0b6c2f10-6f0e-4c3a-9a51-1f4d3e9b7a01",
                    "lecture",
                    1,
                    room="N321",
                    building="ЦТ",
                    host="Анна Королёва",
                    day="monday",
                    start="09:30",
                    end="10:50",
                    first="2026-09-01",
                    last="2026-12-20",
                ),
                _event(
                    "0b6c2f10-6f0e-4c3a-9a51-1f4d3e9b7a02",
                    "seminar",
                    2,
                    room="N214",
                    building="ЦТ",
                    host="Игорь Миронов",
                    day="wednesday",
                    start="11:10",
                    end="12:30",
                    first="2026-09-02",
                    last="2026-12-20",
                ),
            ],
        },
        {
            "courseId": 4102,
            "courseName": "Основы баз данных",
            "eventRows": [
                _event(
                    "0b6c2f10-6f0e-4c3a-9a51-1f4d3e9b7a03",
                    "lecture",
                    1,
                    room=None,
                    building=None,
                    host="Наталья Верещагина",
                    day="tuesday",
                    start="13:00",
                    end="14:20",
                    first="2026-09-01",
                    last="2026-12-20",
                    fmt="online",
                ),
                _event(
                    "0b6c2f10-6f0e-4c3a-9a51-1f4d3e9b7a04",
                    "seminar",
                    2,
                    room="N105",
                    building="ГК",
                    host="Пётр Лебедев",
                    day="friday",
                    start="15:10",
                    end="16:30",
                    first="2026-09-04",
                    last="2026-12-20",
                    interval=2,
                    comment="Каждую вторую неделю",
                ),
            ],
        },
        {
            "courseId": 4103,
            "courseName": "Алгоритмы и структуры данных",
            "eventRows": [
                _event(
                    "0b6c2f10-6f0e-4c3a-9a51-1f4d3e9b7a05",
                    "lecture",
                    1,
                    room="N321",
                    building="ЦТ",
                    host="Сергей Орлов",
                    day="thursday",
                    start="09:30",
                    end="10:50",
                    first="2026-09-03",
                    last="2026-12-20",
                )
            ],
        },
    ]


def default_tasks() -> list[dict[str, Any]]:
    def task(
        number: int,
        state: str,
        name: str,
        deadline: str,
        course: tuple[int, str],
        *,
        score: float | None = None,
        max_score: int = 10,
        submit: str | None = None,
        theme: str = "Тема 1",
    ) -> dict[str, Any]:
        return {
            "id": 9000 + number,
            "state": state,
            "score": score,
            "deadline": deadline,
            "submitAt": submit,
            "exercise": {
                "id": 8000 + number,
                "name": name,
                "type": "homework",
                "maxScore": max_score,
                "deadline": deadline,
            },
            "course": {"id": course[0], "name": course[1], "isArchived": False},
            "theme": {"name": theme},
            "longread": {"name": f"Материалы {number}"},
        }

    algebra, databases = (4101, "Линейная алгебра"), (4102, "Основы баз данных")
    return [
        task(1, "backlog", "Домашнее задание 4", "2026-11-02T20:59:00Z", algebra),
        task(2, "inProgress", "Лабораторная работа 2", "2026-11-09T20:59:00Z", databases),
        task(
            3,
            "review",
            "Контрольная 1",
            "2026-10-30T20:59:00Z",
            algebra,
            submit="2026-10-29T10:15:00Z",
        ),
        task(
            4,
            "failed",
            "Домашнее задание 3",
            "2026-10-20T20:59:00Z",
            databases,
            score=2.0,
            submit="2026-10-19T09:00:00Z",
        ),
        task(
            5,
            "evaluated",
            "Домашнее задание 2",
            "2026-10-12T20:59:00Z",
            algebra,
            score=9.0,
            submit="2026-10-11T18:30:00Z",
        ),
    ]


@dataclass
class LmsState:
    timetables: list[dict[str, Any]] = field(default_factory=default_timetables)
    tasks: list[dict[str, Any]] = field(default_factory=default_tasks)
    # Segments after `/students/` that were asked, and the `state` values of each task read.
    segments: list[str] = field(default_factory=lambda: list[str]())
    state_reads: list[list[str]] = field(default_factory=lambda: list[list[str]]())
    account_reads: int = 0

    def rows(self) -> list[dict[str, Any]]:
        return [row for course in self.timetables for row in course["eventRows"]]


STATE = LmsState()


def reset() -> None:
    STATE.timetables = default_timetables()
    STATE.tasks = default_tasks()
    STATE.segments.clear()
    STATE.state_reads.clear()
    STATE.account_reads = 0


def remove_rule(calendar_event_id: str) -> None:
    """The university drops one repeating rule from its answer."""
    for course in STATE.timetables:
        course["eventRows"] = [
            row
            for row in course["eventRows"]
            if row["calendarEvent"]["calendarEventId"] != calendar_event_id
        ]
    STATE.timetables = [course for course in STATE.timetables if course["eventRows"]]


def add_rule(course_id: int, row: dict[str, Any]) -> None:
    """The university adds a rule to a course of the answer."""
    for course in STATE.timetables:
        if course["courseId"] == course_id:
            course["eventRows"].append(copy.deepcopy(row))
            return
    raise KeyError(course_id)


def new_rule(uid: str, **kw: Any) -> dict[str, Any]:
    return _event(uid, kw.pop("kind", "lecture"), kw.pop("number", 9), **kw)


def _unauthorized() -> JSONResponse:
    return JSONResponse({"title": "Unauthorized", "status": 401}, status_code=401)


def _not_found() -> JSONResponse:
    return JSONResponse({"title": "Not Found", "status": 404}, status_code=404)


@router.get(ME_PATH, response_model=None)
async def account(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return _unauthorized()
    STATE.account_reads += 1
    return JSONResponse({"id": ACCOUNT_ID, "role": "student", "locale": "ru"})


@router.get("/api/lms/students/{who}/timetables", response_model=None)
async def timetables(request: Request, who: str) -> JSONResponse:
    if not is_logged_in(request):
        return _unauthorized()
    STATE.segments.append(who)
    if who != "me":
        return _not_found()
    return JSONResponse(STATE.timetables)


@router.get(TASKS_PATH, response_model=None)
async def tasks(request: Request) -> JSONResponse:
    if not is_logged_in(request):
        return _unauthorized()
    asked = request.query_params.getlist("state")
    STATE.state_reads.append(asked)
    return JSONResponse([task for task in STATE.tasks if task["state"] in asked])


_NAV = (
    f'<nav><a href="{TIMETABLE_PAGE}">Мои пары</a> <a href="{TASKS_PAGE}">Задания</a> '
    '<a href="/auth/logout">Выйти</a></nav>'
)
_DAYS = (
    "{monday:'понедельник',tuesday:'вторник',wednesday:'среда',thursday:'четверг',"
    "friday:'пятница',saturday:'суббота',sunday:'воскресенье'}"
)


def _shell(title: str, script: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><title>{title}</title>{_NAV}"
        '<main id="app" aria-busy="true"></main><script>'
        "const app=document.getElementById('app');"
        f"fetch('{ME_PATH}',{{credentials:'same-origin'}}).then(r=>r.json()).then(me=>{{"
        f"{script}}});</script>"
    )


@router.get("/learn", response_model=None)
async def learn_home(request: Request) -> RedirectResponse:
    target = TIMETABLE_PAGE if is_logged_in(request) else "/auth/login"
    return RedirectResponse(target, status_code=302)


@router.get(TIMETABLE_PAGE, response_model=None)
async def timetable_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not is_logged_in(request):
        return RedirectResponse("/auth/login", status_code=302)
    return _shell(
        "Мои пары",
        # The page is a list of repeating rules: day of the week, time, room and the period.
        f"const days={_DAYS};"
        f"fetch('/api/lms/students/me/timetables',{{credentials:'same-origin'}})"
        ".then(r=>r.json()).then(courses=>{"
        "app.innerHTML='<h1>Мои пары</h1>'+courses.map(c=>'<section><h2>'+c.courseName+'</h2><ul>'"
        "+c.eventRows.map(r=>{const e=r.calendarEvent,s=e.schedule;"
        "return '<li>'+(days[s.dayOfWeek]||s.dayOfWeek)+' '+s.startTime+'–'+s.endTime+' · '"
        "+(e.location?e.location.title+' ('+e.location.building+')':'онлайн')+' · '"
        "+e.hosts.map(h=>h.name).join(', ')+' · с '+s.startDate+' по '+s.endDate+'</li>'}).join('')"
        "+'</ul></section>').join('');app.setAttribute('aria-busy','false');});",
    )


@router.get(TASKS_PAGE, response_model=None)
async def tasks_page(request: Request) -> HTMLResponse | RedirectResponse:
    if not is_logged_in(request):
        return RedirectResponse("/auth/login", status_code=302)
    states = "&".join(f"state={state}" for state in STATES)
    return _shell(
        "Задания",
        f"fetch('{TASKS_PATH}?{states}',{{credentials:'same-origin'}})"
        ".then(r=>r.json()).then(tasks=>{"
        "app.innerHTML='<h1>Задания</h1><ul>'+tasks.map(t=>'<li>'+t.exercise.name+' · '"
        "+t.course.name+' · до '+t.deadline+' · '+t.state+'</li>').join('')+'</ul>';"
        "app.setAttribute('aria-busy','false');});",
    )
