"""Shared helpers for the phase-1 wiring tests (unique name: runtime tests own `runtime_support`).

A tiny local "fakeuni" cabinet on plain loopback HTTP for the real Chromium, plus a services
stand-in for tests that do not need Postgres. No network, no TLS, no proxy.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.clock import SystemClock
from botai.core.queue import InMemoryJobQueue
from botai.core.vault import InMemoryVault
from botai.settings import Settings

# A session factory for wiring tests that never executes a source/rag tool: the engine is v4, so
# the tools must be registered, but the test does not own a database. `db=None` states the opposite
# on purpose and proves the wiring refuses to run without its product collaborators.
_UNSET = object()


def unbound_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(expire_on_commit=False)


SECRET_MARKER = "P@ssw0rd-FIXTURE-MARKER"
SCHEDULE_ROWS = (
    ("Понедельник", "Математический анализ", "10:00"),
    ("Среда", "Базы данных", "13:00"),
)


def create_agent_site() -> FastAPI:
    """The whole "internet" of the wiring tests: a cabinet with a schedule and a few forms."""
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        return HTMLResponse(
            "<html><body><h1>Личный кабинет</h1>"
            "<a id='schedule' href='/cabinet'>Расписание</a></body></html>"
        )

    @app.get("/cabinet", response_class=HTMLResponse)
    async def cabinet() -> HTMLResponse:
        rows = "".join(
            f"<tr id='row-{index}'><td>{day}</td><td>{subject}</td><td>{at}</td></tr>"
            for index, (day, subject, at) in enumerate(SCHEDULE_ROWS)
        )
        return HTMLResponse(
            "<html><body><h1>Личный кабинет</h1>"
            "<table id='schedule'><tr><th>День</th><th>Предмет</th><th>Время</th></tr>"
            f"{rows}</table></body></html>"
        )

    @app.get("/form", response_class=HTMLResponse)
    async def form() -> HTMLResponse:
        return HTMLResponse(
            "<html><body><h1>Вход</h1>"
            "<input id='login' name='login' autocomplete='off'>"
            "<input id='pass' name='pass' type='password'>"
            "<button id='send' onclick=\"document.getElementById('out').textContent = "
            "String(document.getElementById('pass').value.length)\">Войти</button>"
            "<p id='out'></p></body></html>"
        )

    @app.get("/state", response_class=HTMLResponse)
    async def state() -> HTMLResponse:
        return HTMLResponse(
            "<html><body><h1>Состояние</h1><p id='state'>готово</p>"
            "<script>localStorage.setItem('token', 'local-fixture');"
            "document.cookie = 'lab=fixture-cookie; path=/; max-age=3600';</script>"
            "</body></html>"
        )

    @app.get("/whoami")
    async def whoami(request: Request) -> JSONResponse:
        return JSONResponse({"cookies": dict(request.cookies)})

    @app.get("/echo-secret", response_class=HTMLResponse)
    async def echo_secret() -> HTMLResponse:
        return HTMLResponse(
            f"<html><body><h1>Секрет</h1><p id='marker'>{SECRET_MARKER}</p></body></html>"
        )

    @app.get("/download")
    async def download() -> Response:
        return Response(
            content=b"schedule-report-bytes",
            media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="report.txt"'},
        )

    @app.get("/download-link", response_class=HTMLResponse)
    async def download_link() -> HTMLResponse:
        return HTMLResponse("<html><body><a id='dl' href='/download'>Скачать</a></body></html>")

    @app.get("/upload-form", response_class=HTMLResponse)
    async def upload_form() -> HTMLResponse:
        return HTMLResponse(
            "<html><body>"
            "<form action='/upload' method='post' enctype='multipart/form-data'>"
            "<input id='file' type='file' name='file'><button id='send' type='submit'>"
            "Загрузить</button></form></body></html>"
        )

    @app.post("/upload", response_class=HTMLResponse)
    async def upload(file: UploadFile = File(...)) -> HTMLResponse:
        data = await file.read()
        return HTMLResponse(f"<html><body><p id='result'>{len(data)}</p></body></html>")

    return app


@contextlib.asynccontextmanager
async def serve_agent_site() -> AsyncIterator[str]:
    server = uvicorn.Server(
        uvicorn.Config(
            create_agent_site(),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = int(server.servers[0].sockets[0].getsockname()[1])
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def fake_services(
    *,
    settings: Settings | None = None,
    vault: InMemoryVault | None = None,
    db: Any = _UNSET,
) -> Any:
    """A services stand-in for DB-free tests: settings, vault, clock and an in-memory queue."""
    return SimpleNamespace(
        settings=settings or Settings(_env_file=None, env="test", agent_engine="v4"),
        vault=vault or InMemoryVault(),
        clock=SystemClock(),
        browser=None,
        db=unbound_sessionmaker() if db is _UNSET else db,
        queue=InMemoryJobQueue(),
        embedder=None,
        search=None,
        llm=None,
        sessions=None,
    )


async def site_fixture() -> AsyncIterator[tuple[FastAPI, str]]:  # pragma: no cover - helper
    async with serve_agent_site() as base:
        yield create_agent_site(), base


def fixture_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"env": "test", "agent_engine": "v4"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


__all__ = [
    "SCHEDULE_ROWS",
    "SECRET_MARKER",
    "create_agent_site",
    "fake_services",
    "fixture_settings",
    "serve_agent_site",
]
