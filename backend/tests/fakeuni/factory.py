"""A synthetic SPA whose nested schedule needs its own cookie and observed bearer together."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from tests.fakeuni.login import COOKIE, is_logged_in

router = APIRouter()
ENTRY_PATH = "/factory-campus"
DATA_PATH = "/api/factory-week"
STORAGE_KEY = "factoryBearer"
BEARER_PREFIX = "factory-session-bearer-fixture-"


@dataclass
class FactoryState:
    version: int = 1
    successful_reads: int = 0
    denied_reads: int = 0
    # Fingerprints prove that two students used separate sessions, without logging credentials.
    session_fingerprints: set[str] = field(default_factory=set)


STATE = FactoryState()


def reset() -> None:
    STATE.version = 1
    STATE.successful_reads = STATE.denied_reads = 0
    STATE.session_fingerprints.clear()


def bearer_for_cookie(cookie: str) -> str:
    return BEARER_PREFIX + hashlib.sha256(cookie.encode()).hexdigest()[:24]


def schedule() -> dict[str, Any]:
    return {
        "days": [
            {
                "date": date,
                "lessons": [
                    {
                        # A subject repeats on each day; parent date must participate in identity.
                        "id": "algebra",
                        "title": "Алгебра",
                        "begin": "09:30",
                        "end": "10:50",
                        "room": "А-205" if STATE.version > 1 and index == 0 else "А-101",
                    },
                    {
                        "id": "graphs",
                        "title": "Теория графов",
                        "begin": "11:10",
                        "end": "12:30",
                        "room": "Б-204",
                    },
                ],
            }
            for index, date in enumerate(("2026-10-28", "2026-10-29"))
        ]
    }


@router.get(ENTRY_PATH, response_model=None)
async def campus(request: Request) -> HTMLResponse | RedirectResponse:
    if not is_logged_in(request):
        return RedirectResponse("/auth/login", status_code=302)
    bearer = bearer_for_cookie(request.cookies[COOKIE])
    return HTMLResponse(
        "<!doctype html><title>Личный кабинет — Фейкуни</title>"
        '<main><h1>Моё расписание</h1><section id="schedule" aria-busy="true"></section></main>'
        "<script>"
        f"sessionStorage.setItem({json.dumps(STORAGE_KEY)}, {json.dumps(bearer)});"
        f"fetch({json.dumps(DATA_PATH)}, {{headers: {{Authorization: "
        f"'Bearer '+sessionStorage.getItem({json.dumps(STORAGE_KEY)})}}}})"
        ".then(r=>r.json()).then(body=>{"
        "const out=document.querySelector('#schedule');"
        "out.textContent=body.days.flatMap(day=>day.lessons.map(row=>"
        "day.date+' '+row.begin+' '+row.title+' '+row.room)).join('\\n');"
        "out.setAttribute('aria-busy','false');"
        "});</script>"
    )


@router.get(DATA_PATH)
async def factory_week(request: Request) -> JSONResponse:
    cookie = request.cookies.get(COOKIE, "")
    if not is_logged_in(request) or request.headers.get(
        "authorization"
    ) != "Bearer " + bearer_for_cookie(cookie):
        STATE.denied_reads += 1
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    STATE.successful_reads += 1
    STATE.session_fingerprints.add(hashlib.sha256(cookie.encode()).hexdigest())
    return JSONResponse(schedule())
