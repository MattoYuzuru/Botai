"""Owner: W3. JSON API of the synthetic personal cabinet."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from tests.fakeuni import data
from tests.fakeuni.login import is_logged_in

router = APIRouter()


def _unauth() -> JSONResponse:
    return JSONResponse(
        {"title": "Unauthorized", "status": 401},
        status_code=401,
        media_type="application/problem+json",
    )


@router.get("/app", response_class=HTMLResponse)
async def app_shell(request: Request) -> HTMLResponse:
    if not is_logged_in(request):
        return HTMLResponse(status_code=302, headers={"location": "/auth/login"})
    return HTMLResponse(
        "<!doctype html><title>Личный кабинет</title><div id='root'></div>"
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
async def timetable(request: Request) -> JSONResponse | dict[str, object]:
    if not is_logged_in(request):
        return _unauth()
    return {"items": data.timetable()}


@router.get("/api/v2/tasks/deadlines", response_model=None)
async def deadlines(request: Request) -> JSONResponse | list[dict[str, object]]:
    if not is_logged_in(request):
        return _unauth()
    return data.deadlines()
