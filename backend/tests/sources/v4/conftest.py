"""Fixtures of the v4 source service, runner, tools and API: accounts and a fake university."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from botai.settings import Settings, get_settings
from botai.sources.v4 import browser as browser_v4
from botai.sources.v4.artifacts import FileArtifactStore
from botai.sources.v4.router import get_runner_builder
from botai.sources.v4.runner import SourceRunner
from tests.auth.support import *  # noqa: F403
from tests.auth.support import Sm
from tests.fakeuni import login as fakeuni_login
from tests.sources.v4.support import Account, add_space, create_account


@pytest.fixture
async def account(tx_sessionmaker: Sm) -> Account:
    return await create_account(tx_sessionmaker)


@pytest.fixture
async def second_account(tx_sessionmaker: Sm) -> Account:
    return await create_account(tx_sessionmaker, title="Другой вуз")


@pytest.fixture
async def other_space(tx_sessionmaker: Sm, account: Account) -> str:
    return await add_space(tx_sessionmaker, account)


@pytest.fixture
def fakeuni_transport(fakeuni_app: FastAPI) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=fakeuni_app)


@pytest.fixture
def fakeuni_cookies() -> dict[str, str]:
    """A logged-in fakeuni session: the cookie the runner uses as the source's session."""
    token = secrets.token_hex(16)
    fakeuni_login.SESSIONS.add(token)
    return {fakeuni_login.COOKIE: token}


@pytest.fixture
def artifact_store(tmp_path: Path) -> FileArtifactStore:
    return FileArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def v4_settings(settings: Settings) -> Settings:
    """The same test settings with the v4 engine flag on (production switches one env var)."""
    return settings.model_copy(update={"agent_engine": "v4"})


@pytest.fixture
def v4_app(
    app: FastAPI,
    v4_settings: Settings,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
    artifact_store: FileArtifactStore,
) -> FastAPI:
    """The real application with the v4 engine on and fakeuni as the network."""

    def builder(session: AsyncSession, user_id: str) -> SourceRunner:
        return SourceRunner(
            session,
            user_id=user_id,
            transport=fakeuni_transport,
            artifact_store=artifact_store,
            cookies=fakeuni_cookies,
        )

    app.dependency_overrides[get_settings] = lambda: v4_settings
    app.dependency_overrides[get_runner_builder] = lambda: builder
    return app


@pytest.fixture
def v4_client(v4_app: FastAPI, new_client: Callable[[], httpx.AsyncClient]) -> httpx.AsyncClient:
    return new_client()


# --- real browser + loopback fakeuni -----------------------------------------------------------------


class _QuietServer(uvicorn.Server):
    def capture_signals(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


@contextlib.asynccontextmanager
async def _serve(app: FastAPI, host: str) -> AsyncIterator[int]:
    server = _QuietServer(
        uvicorn.Config(
            app, host=host, port=0, log_level="warning", access_log=False, lifespan="off"
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    try:
        yield int(server.servers[0].sockets[0].getsockname()[1])
    finally:
        server.should_exit = True
        await task


@pytest.fixture(scope="session")
async def fakeuni_http(fakeuni_app: FastAPI) -> AsyncIterator[str]:
    """fakeuni on a loopback HTTP origin: a browser test's whole internet, no proxy, no TLS."""
    async with _serve(fakeuni_app, "127.0.0.1") as port:
        yield f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
async def fakeuni_httpx_localhost(fakeuni_app: FastAPI) -> AsyncIterator[str]:
    """The same site under `localhost`: a secure cookie set over HTTP is accepted there."""
    async with _serve(fakeuni_app, "127.0.0.1") as port:
        yield f"http://localhost:{port}"


@pytest.fixture(scope="session")
def browser_factory() -> browser_v4.BrowserFactory:
    """The real wiring factory: a full-control Chromium per browser playbook, session applied."""
    return browser_v4.playwright_factory()
