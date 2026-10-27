"""Owner: W3. JSON API of the synthetic personal cabinet.

`STATE` lets a test change what the university serves (an endpoint moves, markup changes, the
session dies) without touching the app; `reset()` restores the defaults between tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from tests.fakeuni import data, ical, moodle
from tests.fakeuni.login import SESSIONS, is_logged_in

router = APIRouter()
router.include_router(moodle.router)
router.include_router(ical.router)


@dataclass
class FakeState:
    # "v2": /api/v2/timetable. "v3": the endpoint moved to /api/v3/schedule, same payload.
    timetable_version: str = "v2"
    # "v1": li.news-item markup. "v2": the same list in article.post markup.
    news_markup: str = "v1"
    # "v1" or "v2" of the exam list served at /files/exams.xlsx (one exam moves in v2).
    exams_version: str = "v1"
    # When set, every API call answers with this status (503 to test outages).
    fail_status: int | None = None


STATE = FakeState()


def reset() -> None:
    STATE.timetable_version = "v2"
    STATE.news_markup = "v1"
    STATE.exams_version = "v1"
    STATE.fail_status = None
    ical.reset()


def expire_sessions() -> None:
    """The university forgets every logged-in session (the cookie value stays valid in the jar)."""
    SESSIONS.clear()


def _unauth() -> JSONResponse:
    return JSONResponse(
        {"title": "Unauthorized", "status": 401},
        status_code=401,
        media_type="application/problem+json",
    )


def _down() -> JSONResponse | None:
    if STATE.fail_status:
        return JSONResponse({"title": "down"}, status_code=STATE.fail_status)
    return None


@router.get("/app", response_class=HTMLResponse)
async def app_shell(request: Request) -> HTMLResponse:
    if not is_logged_in(request):
        return HTMLResponse(status_code=302, headers={"location": "/auth/login"})
    return HTMLResponse(
        "<!doctype html><title>Личный кабинет</title><div id='root'></div>"
        "<nav><a href='/app'>Главная</a> <a href='/portal/deadlines'>Задания</a> "
        "<a href='/news'>Новости</a> <a href='/api/docs'>API</a> "
        "<a href='/auth/logout'>Выйти</a></nav>"
        "<script>fetch('/api/v2/me');fetch('/api/v2/timetable?from=2026-10-26&to=2026-11-01');"
        "fetch('/api/v2/tasks/deadlines')</script>"
    )


@router.get("/api/v2/me", response_model=None)
async def me(request: Request) -> JSONResponse | dict[str, object]:
    if not is_logged_in(request):
        return _unauth()
    return {
        "sub": "u-1001",
        "fullName": "Иванова Анна",
        "programme": "Прикладная математика",
        "year": 3,
    }


@router.get("/api/v2/timetable", response_model=None)
async def timetable(request: Request) -> Response | dict[str, object]:
    if not is_logged_in(request):
        return _unauth()
    if (down := _down()) is not None:
        return down
    if STATE.timetable_version != "v2":
        return JSONResponse({"title": "Not Found", "status": 404}, status_code=404)
    return {"items": data.timetable()}


@router.get("/api/v3/schedule", response_model=None)
async def schedule_v3(request: Request) -> Response | dict[str, object]:
    if not is_logged_in(request):
        return _unauth()
    if STATE.timetable_version != "v3":
        return JSONResponse({"title": "Not Found", "status": 404}, status_code=404)
    return {"items": data.timetable()}


@router.get("/api/v2/tasks/deadlines", response_model=None)
async def deadlines(request: Request) -> Response | list[dict[str, object]]:
    if not is_logged_in(request):
        return _unauth()
    if (down := _down()) is not None:
        return down
    return data.deadlines()


COURSES = [
    {"id": f"c-{i}", "name": name}
    for i, name in enumerate(
        ["Математический анализ", "Алгоритмы", "Базы данных", "Английский язык", "Физика"], 1
    )
]


@router.get("/api/v2/courses", response_model=None)
async def courses(request: Request, page: int = 1, size: int = 2) -> Response | dict[str, object]:
    """Page-numbered pagination: `page` from 1, `size` per page, `total` of all."""
    if not is_logged_in(request):
        return _unauth()
    start = (page - 1) * size
    return {"total": len(COURSES), "results": COURSES[start : start + size]}


@router.get("/api/v2/feed", response_model=None)
async def feed(request: Request, cursor: str | None = None) -> Response | dict[str, object]:
    """Cursor pagination: `next` is the cursor of the following page, null at the end."""
    if not is_logged_in(request):
        return _unauth()
    pages = {
        None: ([{"id": "n-1", "title": "Первая"}, {"id": "n-2", "title": "Вторая"}], "p2"),
        "p2": ([{"id": "n-3", "title": "Третья"}], None),
    }
    items, nxt = pages.get(cursor, ([], None))
    return {"items": items, "next": nxt}
