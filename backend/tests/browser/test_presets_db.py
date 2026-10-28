"""PgLoginStore against the migrated schema (marker `db`)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from botai.browser.presets import InMemoryLoginStore, PgLoginStore, normalize_origin
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


async def test_approved_host_is_merged_only_into_its_owners_connection(db: AsyncSession) -> None:
    user, conn = await make_connection(db)
    other_user, other_conn = await make_connection(db)
    store = store_on(db)
    with pytest.raises(BotaiError) as exc:
        await store.allow_host(other_user, conn, "id.fakeuni.test")
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert (await store.connection(user, conn)).allow_hosts == ("fakeuni.test",)
    await store.allow_host(user, conn, "ID.Fakeuni.test.")
    await store.allow_host(user, conn, "login.fakeuni.test")
    await store.allow_host(user, conn, "id.fakeuni.test")
    assert (await store.connection(user, conn)).allow_hosts == (
        "fakeuni.test",
        "id.fakeuni.test",
        "login.fakeuni.test",
    )
    assert (await store.connection(other_user, other_conn)).allow_hosts == ("fakeuni.test",)


async def test_disabled_connection_rejects_a_late_host_approval_without_changing_hosts(
    db: AsyncSession,
) -> None:
    user, conn = await make_connection(db)
    store = store_on(db)
    assert (await store.connection(user, conn)).allow_hosts == ("fakeuni.test",)
    # The connection is disabled while the confirmation is still on the screen.
    await db.execute(
        update(Connection)
        .where(Connection.id == conn, Connection.user_id == user)
        .values(status="disabled")
    )
    await db.flush()
    with pytest.raises(BotaiError, match="Подключение отключено") as approval:
        await store.allow_host(user, conn, "id.fakeuni.test")
    assert approval.value.code == ErrorCode.FORBIDDEN
    with pytest.raises(BotaiError, match="Подключение отключено") as read:
        await store.connection(user, conn)
    assert read.value.code == ErrorCode.FORBIDDEN
    hosts = (
        await db.execute(
            select(Connection.allow_hosts).where(Connection.id == conn, Connection.user_id == user)
        )
    ).scalar_one()
    assert hosts == ["fakeuni.test"]


async def test_in_memory_host_approval_keeps_the_same_tenant_and_merge_rules() -> None:
    store = InMemoryLoginStore()
    store.add_connection("a", "one", "https://fakeuni.test")
    store.add_connection("b", "two", "https://fakeuni.test")
    with pytest.raises(BotaiError) as exc:
        await store.allow_host("b", "one", "id.fakeuni.test")
    assert exc.value.code == ErrorCode.NOT_FOUND
    await store.allow_host("a", "one", "ID.Fakeuni.test.")
    await store.allow_host("a", "one", "id.fakeuni.test")
    assert (await store.connection("a", "one")).allow_hosts == ("fakeuni.test", "id.fakeuni.test")
    assert (await store.connection("b", "two")).allow_hosts == ("fakeuni.test",)


async def test_concurrent_approved_hosts_preserve_both_updates(engine: AsyncEngine) -> None:
    """Separate workers may approve distinct hosts: merge under the connection's row lock."""
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        user, conn = await make_connection(session)
        await session.commit()
    store = PgLoginStore(sm)
    try:
        await asyncio.gather(
            store.allow_host(user, conn, "id.fakeuni.test"),
            store.allow_host(user, conn, "login.fakeuni.test"),
        )
        assert set((await store.connection(user, conn)).allow_hosts) == {
            "fakeuni.test",
            "id.fakeuni.test",
            "login.fakeuni.test",
        }
    finally:
        # Only this test's user in the throwaway test database, including its connection.
        async with sm() as session:
            await session.execute(delete(User).where(User.id == user))
            await session.commit()


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
