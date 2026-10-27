"""Database seeding and service wiring for sync, learning and repair tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.browser import FakeBrowserGateway, SessionState
from botai.core.clock import FrozenClock
from botai.core.embed import NoopEmbedder
from botai.core.errors import BotaiError
from botai.core.ids import new_id
from botai.core.llm import ScriptedLlm
from botai.core.queue import InMemoryJobQueue
from botai.core.services import Services
from botai.core.vault import InMemoryVault, Vault
from botai.db import models as m
from botai.settings import Settings
from tests.fakeuni import data
from tests.sources import recipes
from tests.sources.helpers import NOW, new_session_token, session_state


class FakeSessions:
    """SessionProvider double: `ensure` logs in again on the fakeuni (or fails on request)."""

    def __init__(self, *, works: bool = True, fail: BotaiError | None = None) -> None:
        self.works = works
        self.fail = fail
        self.calls = 0

    async def ensure(self, ctx: object, *, user_id: str, connection_id: str) -> SessionState:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        token = new_session_token(f"relogin-{self.calls}") if self.works else "not-a-session"
        return session_state(token)


def make_services(
    sm: async_sessionmaker[AsyncSession],
    *,
    vault: Vault | None = None,
    llm: ScriptedLlm | None = None,
    sessions: FakeSessions | None = None,
    queue: InMemoryJobQueue | None = None,
    browser: FakeBrowserGateway | None = None,
    clock: FrozenClock | None = None,
) -> Services:
    return Services(
        settings=Settings(_env_file=None),  # pyright: ignore[reportCallIssue]
        db=sm,
        clock=clock or FrozenClock(NOW),
        vault=vault or InMemoryVault(),
        llm=llm or ScriptedLlm(),
        browser=browser or FakeBrowserGateway({}, NOW, data.ORIGIN),
        sessions=sessions or FakeSessions(),
        queue=queue or InMemoryJobQueue(),
        embedder=NoopEmbedder(),
    )


@dataclass(slots=True)
class Seeded:
    user_id: str
    space_id: str
    connection_id: str
    spec_id: str


async def seed_user(db: AsyncSession) -> str:
    user = m.User(
        id=new_id(), email=f"{new_id().lower()}@botai.test", display_name="Тест", password_hash="x"
    )
    db.add(user)
    await db.flush()
    return user.id


async def seed_connection(
    db: AsyncSession,
    *,
    user_id: str | None = None,
    spec: dict[str, Any] | None = None,
    origin: str = data.ORIGIN,
    adapter: str | None = None,
    status: str = "active",
    allow_hosts: list[str] | None = None,
    preset: dict[str, Any] | None = None,
    time_zone: str = "Europe/Moscow",
    profile: dict[str, Any] | None = None,
    attach_spec: bool = True,
    spec_state: str = "active",
    kind: str = "portal",
) -> Seeded:
    uid = user_id or await seed_user(db)
    spec = spec or recipes.recipe_spec()
    kind_of_spec = adapter or str(spec["adapter"])
    revision = int(spec["recipe"]["revision"]) if kind_of_spec == "recipe" else 1
    space = m.Space(
        id=new_id(),
        user_id=uid,
        title="Фейкуни",
        time_zone=time_zone,
        profile=profile or {"name": "Иванова Анна Сергеевна"},
    )
    db.add(space)
    await db.flush()
    spec_row = (
        await db.execute(
            select(m.AdapterSpecRow).where(
                m.AdapterSpecRow.origin == origin,
                m.AdapterSpecRow.adapter == kind_of_spec,
                m.AdapterSpecRow.revision == revision,
            )
        )
    ).scalar_one_or_none()
    if spec_row is None:  # a university's students share one learned spec
        spec_row = m.AdapterSpecRow(
            id=new_id(),
            origin=origin,
            adapter=kind_of_spec,
            revision=revision,
            state=spec_state,
            spec=spec,
        )
        db.add(spec_row)
    preset_id: str | None = None
    if preset is not None:
        row = (
            await db.execute(
                select(m.LoginPresetRow).where(
                    m.LoginPresetRow.origin == origin, m.LoginPresetRow.revision == 1
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = m.LoginPresetRow(
                id=new_id(), origin=origin, revision=1, state="active", preset=preset
            )
            db.add(row)
        preset_id = row.id
    await db.flush()
    conn = m.Connection(
        id=new_id(),
        user_id=uid,
        space_id=space.id,
        origin=origin,
        kind=kind,
        adapter=kind_of_spec,
        title="Кабинет",
        status=status,
        spec_id=spec_row.id if attach_spec else None,
        spec_revision=revision if attach_spec else None,
        login_preset_id=preset_id,
        allow_hosts=allow_hosts if allow_hosts is not None else ["fakeuni.test"],
    )
    db.add(conn)
    await db.flush()
    return Seeded(uid, space.id, conn.id, spec_row.id)
