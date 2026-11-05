"""The chat-share migration on a disposable database: table, one link per thread, cascades, rollback.

Additive: one new table. A deleted thread or user takes its links with it, and the downgrade drops
exactly that table.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.db
BACKEND = Path(__file__).resolve().parents[2]
BEFORE = "c3a1f0d2e4b5"
REVISION = "e6f3a9b1c2d4"
TABLE = "chat_shares"


async def test_chat_shares_migration_round_trip(database_url: str) -> None:
    name = f"botai_chat_shares_{uuid.uuid4().hex[:10]}"
    root_url, _, _ = database_url.rpartition("/")
    target_url = f"{root_url}/{name}"
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "src/botai/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", target_url)
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision(REVISION)
    assert revision is not None
    assert revision.down_revision == BEFORE

    async def execute(sql: str) -> list[tuple[object, ...]]:
        engine = create_async_engine(target_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql))
                return [tuple(row) for row in result] if result.returns_rows else []
        finally:
            await engine.dispose()

    async def tables() -> set[object]:
        rows = await execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in rows}

    try:
        await asyncio.to_thread(command.upgrade, cfg, BEFORE)
        assert TABLE not in await tables()
        await execute(
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('u1','u1@fakeuni.test','Fixture','x',false,false)"
        )
        await execute(
            "INSERT INTO chat_threads (id,user_id,title,last_message_at) "
            "VALUES ('t1','u1','Чат',now()), ('t2','u1','Чат',now())"
        )

        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        await execute("INSERT INTO chat_shares (id,thread_id,user_id) VALUES ('s1','t1','u1')")
        with pytest.raises(IntegrityError):  # one link per thread
            await execute("INSERT INTO chat_shares (id,thread_id,user_id) VALUES ('s2','t1','u1')")
        # A deleted thread takes its link with it.
        await execute("DELETE FROM chat_threads WHERE id = 't1'")
        assert await execute("SELECT count(*) FROM chat_shares") == [(0,)]
        await execute("INSERT INTO chat_shares (id,thread_id,user_id) VALUES ('s3','t2','u1')")
        # A deleted user takes everything (owner data).
        await execute("DELETE FROM users WHERE id = 'u1'")
        assert await execute("SELECT count(*) FROM chat_shares") == [(0,)]

        await asyncio.to_thread(command.downgrade, cfg, BEFORE)
        left = await tables()
        assert TABLE not in left
        assert {"users", "chat_threads", "chat_messages"} <= left
        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        assert await execute("SELECT version_num FROM alembic_version") == [(REVISION,)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
