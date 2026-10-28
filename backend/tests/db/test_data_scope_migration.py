"""Upgrade/rollback of the additive scope column on a disposable database only."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.db
BACKEND = Path(__file__).resolve().parents[2]


async def test_scope_migration_preserves_existing_connection_and_can_roll_back(
    database_url: str,
) -> None:
    name = f"botai_scope_migration_{uuid.uuid4().hex[:10]}"
    root_url, _, _ = database_url.rpartition("/")
    target_url = f"{root_url}/{name}"
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "src/botai/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", target_url)

    async def execute(sql: str) -> list[tuple[object, ...]]:
        engine = create_async_engine(target_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql))
                return [tuple(row) for row in result] if result.returns_rows else []
        finally:
            await engine.dispose()

    try:
        await asyncio.to_thread(command.upgrade, cfg, "8148f6a927c3")
        await execute(
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('scope-user','migration@fakeuni.test','Fixture','unusable-hash',false,false)"
        )
        await execute(
            "INSERT INTO spaces (id,user_id,title,accent_hue,time_zone,profile) "
            "VALUES ('scope-space','scope-user','Fixture',262.2,'Europe/Moscow','{}')"
        )
        await execute(
            "INSERT INTO connections (id,user_id,space_id,origin,kind,title,status,allow_hosts) "
            "VALUES ('scope-source','scope-user','scope-space','https://fakeuni.test',"
            "'lms','Fixture','draft','[]')"
        )
        await asyncio.to_thread(command.upgrade, cfg, "head")
        assert await execute("SELECT title, data_scope FROM connections") == [("Fixture", None)]
        await execute("UPDATE connections SET data_scope='[\"scheduleEvents\"]'::jsonb")
        assert await execute("SELECT data_scope FROM connections") == [(["scheduleEvents"],)]
        await asyncio.to_thread(command.downgrade, cfg, "8148f6a927c3")
        assert await execute("SELECT title, status FROM connections") == [("Fixture", "draft")]
        assert (
            await execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='connections' AND column_name='data_scope'"
            )
            == []
        )
        await asyncio.to_thread(command.upgrade, cfg, "head")
        assert await execute("SELECT title, data_scope FROM connections") == [("Fixture", None)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
