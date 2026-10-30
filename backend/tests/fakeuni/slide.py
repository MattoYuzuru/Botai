"""A synthetic cabinet whose API answers a sliding window of 7 days from `begin_date`.

The page `/schedule?begin_date=…` is a single-page app that calls the API for the same date. The
week of the control work has one class, the weeks around it are full, like a real timetable:
what a recipe sees depends on which window it asks for. `STATE` records the dates asked and can
make the whole timetable thin to test the question to the student.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from tests.fakeuni.login import is_logged_in

router = APIRouter()

API_PATH = "/api/get-myschedule"
PAGE_PATH = "/schedule"
FAQ_PATH = "/api/get-faq"
CONTROL_WEEK = date(2026, 10, 26)  # the week of the control work: one class
CONTROL_DAY = date(2026, 10, 28)
FIRST_DAY = date(2026, 10, 12)
LAST_DAY = date(2026, 12, 6)
GROUP = "ГРП 3 курс 26/27"
CONTROL_KIND = "Контрольное мероприятие"
ADDRESS = "Учебная ул., д. 5"
BUILDING = "Корпус 2"
SUBJECTS = ("Теория вероятностей", "Дискретная математика", "Алгоритмы", "Базы данных")


@dataclass
class SlideState:
    sparse: bool = False  # only the control-work class exists, in every window
    asked: list[str] = field(default_factory=lambda: list[str]())  # begin_date of each read
    pages: list[str] = field(default_factory=lambda: list[str]())  # begin_date of each page


STATE = SlideState()


def reset() -> None:
    STATE.sparse = False
    STATE.asked.clear()
    STATE.pages.clear()


def lesson(day: date, number: int, name: str, kind: str, room: str) -> dict[str, Any]:
    starts = ("09:30", "11:10", "13:00", "14:40")[number - 1]
    ends = ("10:50", "12:30", "14:20", "16:00")[number - 1]
    return {
        "auditorium": room,
        "building": ADDRESS,
        "name": name,
        "lesson_type": kind,
        "lesson_type_id": 7 if kind == CONTROL_KIND else 2,
        "begin_time": starts,
        "end_time": ends,
        "lecturer": "Петров П.П.",
        "lesson_number": number,
        "parent_schedule": GROUP,
        "url1": f"https://meet.fakeuni.test/room/{day.isoformat()}-{number}"
        if number == 1
        else None,
        "url1_description": "Онлайн-комната" if number == 1 else None,
        "url2": None,
        "url2_description": None,
    }


def day_lessons(day: date) -> list[dict[str, Any]]:
    if not FIRST_DAY <= day <= LAST_DAY or day.weekday() > 4:
        return []
    if STATE.sparse or CONTROL_WEEK <= day < CONTROL_WEEK + timedelta(days=7):
        return (
            [lesson(day, 2, "Контрольная работа", CONTROL_KIND, "Б-204")]
            if day == CONTROL_DAY
            else []
        )
    count = 1 if day.weekday() == 4 else 2
    return [
        lesson(
            day,
            number,
            SUBJECTS[(day.weekday() + number) % len(SUBJECTS)],
            "Семинар",
            f"Б-{200 + number}",
        )
        for number in range(1, count + 1)
    ]


def window(begin: date) -> list[dict[str, Any]]:
    """Seven days from `begin`; `end_date` is not a parameter, the window is the source's own."""
    return [
        {"date": (day := begin + timedelta(days=offset)).isoformat(), "lessons": day_lessons(day)}
        for offset in range(7)
    ]


def _unauthorized() -> JSONResponse:
    return JSONResponse({"title": "Unauthorized", "status": 401}, status_code=401)


@router.get(API_PATH, response_model=None)
async def my_schedule(request: Request, begin_date: str) -> JSONResponse:
    if not is_logged_in(request):
        return _unauthorized()
    try:
        begin = date.fromisoformat(begin_date)
    except ValueError:
        return JSONResponse({"title": "Bad date"}, status_code=422)
    STATE.asked.append(begin_date)
    return JSONResponse(window(begin))


@router.get(FAQ_PATH, response_model=None)
async def faq(request: Request, q: str = "") -> JSONResponse:
    if not is_logged_in(request):
        return _unauthorized()
    return JSONResponse([{"question": "Как перенести занятие?", "answer": "Через деканат."}])


@router.get(PAGE_PATH, response_model=None)
async def schedule_page(request: Request, begin_date: str = "") -> HTMLResponse | RedirectResponse:
    if not is_logged_in(request):
        return RedirectResponse("/auth/login", status_code=302)
    STATE.pages.append(begin_date)
    return HTMLResponse(
        "<!doctype html><title>Расписание</title>"
        '<nav><a href="/schedule">Расписание</a> <a href="/faq">Частые вопросы</a> '
        '<a href="/auth/logout">Выйти</a></nav>'
        '<main><h1>Моё расписание</h1><ul id="lessons"></ul></main><script>'
        "const q=location.search;"
        f"fetch('{API_PATH}'+q).then(r=>r.json()).then(days=>{{"
        "const out=document.getElementById('lessons');"
        "for(const d of days)for(const l of d.lessons)"
        "out.insertAdjacentHTML('beforeend',`<li>${d.date} ${l.begin_time} ${l.name}</li>`);"
        "});"
        f"fetch('{FAQ_PATH}?q=');"
        "</script>"
    )
