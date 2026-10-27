"""A real, committed environment for job tests: several connections, workers, a live server.

Job tests cannot use the rolled-back transaction of the other W1 tests: a worker, the API and a
second worker are separate connections that must see each other's commits (SKIP LOCKED, NOTIFY).
Every user a test creates is removed afterwards (foreign keys cascade to spaces, connections,
jobs, events, answers and secrets).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from botai.api.deps import get_clock, get_services
from botai.core.browser import FakeBrowserGateway
from botai.core.clock import FrozenClock
from botai.core.embed import NoopEmbedder
from botai.core.handlers import HandlerTable
from botai.core.llm import ScriptedLlm
from botai.core.services import Services
from botai.core.vault import generate_kek_b64, load_kek
from botai.db.models import Job, JobAnswer, JobEvent, University, User
from botai.db.session import db_session
from botai.jobs.notify import Notifier, dsn_for_asyncpg, stop_shared_notifiers
from botai.jobs.queue import PgJobQueue
from botai.jobs.runner import RunnerConfig, Worker
from botai.main import create_app
from botai.settings import Settings, get_settings
from botai.vault.pg import PgVault
from tests.auth.support import (
    NOW,
    PASSWORD,
    Account,
    make_connection,
    make_space,
    make_user,
    signup,
)

Sm = async_sessionmaker[AsyncSession]


@dataclass
class Env:
    engine: AsyncEngine
    sm: Sm
    clock: FrozenClock
    settings: Settings
    services: Services
    app: FastAPI
    new_client: Callable[[], httpx.AsyncClient]
    users: list[str] = field(default_factory=list)
    workers: list[tuple[Worker, asyncio.Task[None]]] = field(default_factory=list)
    notifiers: list[Notifier] = field(default_factory=list)
    _owner: str | None = None

    async def owner(self) -> str:
        if self._owner is None:
            self._owner = await make_user(self.sm, is_owner=True)
            self.users.append(self._owner)
        return self._owner

    async def account(
        self, client: httpx.AsyncClient | None = None, email: str | None = None
    ) -> Account:
        """A registered student, logged in on `client`."""
        c = client or self.new_client()
        acc = await signup(c, self.sm, self.clock, email=email, owner_id=await self.owner())
        self.users.append(acc.id)
        return acc

    async def connection(
        self, user_id: str, *, status: str = "active", **kw: Any
    ) -> tuple[str, str]:
        """(space_id, connection_id) owned by `user_id`."""
        space = await make_space(self.sm, user_id)
        return space, await make_connection(self.sm, user_id, space, status=status, **kw)

    def worker(
        self,
        handlers: HandlerTable,
        *,
        name: str | None = None,
        notify: bool = True,
        services: Services | None = None,
        **config: Any,
    ) -> Worker:
        cfg = RunnerConfig(
            poll_seconds=config.pop("poll_seconds", 0.05),
            heartbeat_seconds=config.pop("heartbeat_seconds", 3600),
            maintenance_seconds=config.pop("maintenance_seconds", 3600),
            scheduler_seconds=config.pop("scheduler_seconds", 3600),
            **config,
        )
        notifier = Notifier(dsn_for_asyncpg(self.settings.database_url) if notify else None)
        self.notifiers.append(notifier)
        return Worker(handlers, services or self.services, config=cfg, notifier=notifier, name=name)

    def start(self, worker: Worker) -> asyncio.Task[None]:
        task = asyncio.create_task(worker.run())
        self.workers.append((worker, task))
        return task

    async def job(self, job_id: str) -> Job:
        async with self.sm() as s:
            job = await s.get(Job, job_id)
            assert job is not None
            return job

    async def events(self, job_id: str) -> list[JobEvent]:
        async with self.sm() as s:
            rows = await s.scalars(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.seq)
            )
            return list(rows)

    async def answers(self, job_id: str) -> list[JobAnswer]:
        async with self.sm() as s:
            return list(await s.scalars(select(JobAnswer).where(JobAnswer.job_id == job_id)))

    async def wait_for(self, job_id: str, states: set[str], *, within: float = 10.0) -> Job:
        deadline = asyncio.get_running_loop().time() + within
        while True:
            job = await self.job(job_id)
            if job.state in states:
                return job
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"job {job_id} is {job.state!r}, wanted one of {sorted(states)}"
                )
            await asyncio.sleep(0.02)

    def advance(self, **delta: float) -> None:
        self.clock.advance(timedelta(**delta))


@pytest.fixture
async def env(engine: AsyncEngine, database_url: str) -> AsyncIterator[Env]:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    clock = FrozenClock(NOW)
    settings = Settings(env="test", botai_kek=generate_kek_b64(), database_url=database_url)  # type: ignore[arg-type]
    vault = PgVault(
        sm,
        {settings.botai_kek_id: load_kek(settings.botai_kek.get_secret_value())},
        settings.botai_kek_id,
        clock=clock,
    )
    services = Services(
        settings=settings,
        db=sm,
        clock=clock,
        vault=vault,
        llm=ScriptedLlm(),
        browser=FakeBrowserGateway({}, NOW, "https://fake.invalid"),
        sessions=cast(Any, None),
        queue=PgJobQueue(sm, clock),
        embedder=NoopEmbedder(),
    )
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    app.dependency_overrides[db_session] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_services] = lambda: services

    clients: list[httpx.AsyncClient] = []

    def new_client() -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")
        clients.append(c)
        return c

    async with sm() as s:
        universities_before = set(await s.scalars(select(University.id)))
    e = Env(engine, sm, clock, settings, services, app, new_client)
    try:
        yield e
    finally:
        for worker, _ in e.workers:
            worker.stop()
        for _, task in e.workers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, 10)
        for n in e.notifiers:
            await n.stop()
        await stop_shared_notifiers()
        for c in clients:
            await c.aclose()
        async with sm() as s:
            await s.execute(delete(User).where(User.id.in_(e.users)))
            # the registry is shared and not owned by a user: remove what the test taught it
            await s.execute(delete(University).where(University.id.not_in(universities_before)))
            await s.commit()


@contextlib.asynccontextmanager
async def live_server(app: FastAPI) -> AsyncIterator[str]:
    """The app on a real socket, for tests of streaming (the ASGI test transport buffers)."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@contextlib.asynccontextmanager
async def live_http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client for `live_server`; long timeouts, streams are expected to stay open."""
    async with live_server(app) as base, httpx.AsyncClient(base_url=base, timeout=30) as http:
        yield http


def session_header(client: httpx.AsyncClient) -> dict[str, str]:
    """The Cookie header of a logged-in test client (cookies are `Secure` in the test settings)."""
    return {"Cookie": f"botai_session={client.cookies['botai_session']}"}


__all__ = ["PASSWORD", "Env", "env", "live_http", "live_server", "session_header"]
