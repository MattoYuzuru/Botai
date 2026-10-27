"""PgLoginStore against the migrated schema (marker `db`)."""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.browser.presets import PgLoginStore, normalize_origin
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.db.models import Connection, LoginPresetRow, Space, User
from tests.browser.test_login import preset_dict

pytestmark = pytest.mark.db


class _Scope:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> None:
        return None


def store_on(session: AsyncSession) -> PgLoginStore:
    """A store that works inside the test's rolled-back transaction."""
    return PgLoginStore(cast(async_sessionmaker[AsyncSession], lambda: _Scope(session)))


async def make_connection(
    session: AsyncSession, origin: str = "https://Fakeuni.test:443/"
) -> tuple[str, str]:
    user = User(id=new_id(), email=f"{new_id()}@t.test", display_name="t", password_hash="x")
    session.add(user)
    await session.flush()
    space = Space(id=new_id(), user_id=user.id, title="Вуз")
    session.add(space)
    await session.flush()
    conn = Connection(
        id=new_id(),
        user_id=user.id,
        space_id=space.id,
        origin=origin,
        kind="portal",
        title="Кабинет",
        allow_hosts=["fakeuni.test"],
    )
    session.add(conn)
    await session.flush()
    return user.id, conn.id


def test_origin_is_normalised() -> None:
    assert normalize_origin("https://Fakeuni.test:443/path?q=1") == "https://fakeuni.test"
    assert normalize_origin("fakeuni.test") == "https://fakeuni.test"
    assert normalize_origin("https://fakeuni.test:8443") == "https://fakeuni.test:8443"


async def test_connection_is_scoped_to_its_owner(db: AsyncSession) -> None:
    user, conn = await make_connection(db)
    store = store_on(db)
    info = await store.connection(user, conn)
    assert info.origin == "https://fakeuni.test"
    assert info.allow_hosts == ("fakeuni.test",)
    assert info.preset_id is None
    with pytest.raises(BotaiError) as exc:
        await store.connection(new_id(), conn)
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_presets_get_revisions_and_the_connection_follows_the_latest(
    db: AsyncSession,
) -> None:
    user, conn = await make_connection(db)
    store = store_on(db)
    origin = "https://fakeuni.test"
    assert await store.preset_for(origin) is None

    first: dict[str, Any] = preset_dict(f"{origin}/")
    assert await store.save_preset(origin, first, connection_id=conn) == 1
    loaded = await store.preset_for(origin)
    assert loaded is not None
    assert loaded.revision == 1
    assert loaded.allow_hosts == ["fakeuni.test"]

    second = {**first, "allowHosts": ["fakeuni.test", "id.fakeuni.test"]}
    assert await store.save_preset(origin, second, connection_id=conn) == 2
    latest = await store.preset_for(origin)
    assert latest is not None
    assert latest.revision == 2
    assert latest.allow_hosts == ["fakeuni.test", "id.fakeuni.test"]

    rows = (
        await db.execute(select(LoginPresetRow).where(LoginPresetRow.origin == origin))
    ).scalars()
    assert {row.revision: row.state for row in rows} == {1: "deprecated", 2: "active"}
    info = await store.connection(user, conn)
    assert info.preset_id is not None
