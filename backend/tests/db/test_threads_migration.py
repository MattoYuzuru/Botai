"""Threads lose purpose/connect_context and gain read state; old messages count as read."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.db
BACKEND = Path(__file__).resolve().parents[2]
PREVIOUS = "b5d1c8e7a42f"  # the chain: WS4 sources, WS2 event card, then the threads
REVISION = "a7d41c9e5b20"


async def test_thread_migration_backfills_read_state_and_can_roll_back(database_url: str) -> None:
    name = f"botai_threads_migration_{uuid.uuid4().hex[:10]}"
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

    async def columns(table: str) -> set[str]:
        rows = await execute(
            "SELECT column_name FROM information_schema.columns "  # noqa: S608 - fixed table names
            f"WHERE table_name='{table}'"
        )
        return {str(row[0]) for row in rows}

    try:
        await asyncio.to_thread(command.upgrade, cfg, PREVIOUS)
        assert {"purpose", "connect_context", "updated_at"} <= await columns("chat_threads")
        for sql in (
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('u1','m@fakeuni.test','Fixture','unusable-hash',false,false)",
            "INSERT INTO chat_threads (id,user_id,title,purpose,connect_context,created_at,updated_at) "
            "VALUES ('t-old','u1','Подключение','connect','{\"connection_id\":\"c1\"}',"
            "'2026-10-01T10:00:00Z','2026-10-01T10:00:00Z')",
            "INSERT INTO chat_threads (id,user_id,title,created_at,updated_at) "
            "VALUES ('t-empty','u1','Пустой','2026-10-02T10:00:00Z','2026-10-02T10:00:00Z')",
            "INSERT INTO chat_messages (id,user_id,thread_id,role,blocks,created_at) VALUES "
            "('m1','u1','t-old','user','[]','2026-10-01T10:05:00Z'),"
            "('m2','u1','t-old','agent','[]','2026-10-01T10:09:00Z')",
            "INSERT INTO jobs (id,user_id,kind,state,thread_id,payload,attempts,run_after) VALUES "
            "('j-ok','u1','connect','done','t-old','{}',0,now()),"
            "('j-lost','u1','connect','done','t-gone','{}',0,now())",
        ):
            await execute(sql)
        await asyncio.to_thread(command.upgrade, cfg, REVISION)

        assert not {"purpose", "connect_context", "updated_at"} & await columns("chat_threads")
        rows = await execute(
            "SELECT id, last_message_at = '2026-10-01T10:09:00Z', last_read_at = last_message_at "
            "FROM chat_threads ORDER BY id"
        )
        # the last message of a thread, or its creation time when it has none; all already read
        assert rows == [("t-empty", False, True), ("t-old", True, True)]
        assert await execute(
            "SELECT last_message_at = created_at FROM chat_threads WHERE id='t-empty'"
        ) == [(True,)]
        assert await execute("SELECT seen_links::text FROM chat_threads WHERE id='t-old'") == [
            ("{}",)
        ]
        # a task keeps its thread; one that pointed at nothing is detached instead of failing
        assert await execute("SELECT id, thread_id FROM jobs ORDER BY id") == [
            ("j-lost", None),
            ("j-ok", "t-old"),
        ]
        assert await columns("user_memory") == {"id", "user_id", "note", "created_at"}
        await execute(
            "INSERT INTO user_memory (id,user_id,note) VALUES ('n1','u1','группа БПМ-21')"
        )
        with pytest.raises(IntegrityError):  # the same note, whatever its case, is kept once
            await execute(
                "INSERT INTO user_memory (id,user_id,note) VALUES ('n2','u1','Группа бпм-21')"
            )
        await execute("DELETE FROM chat_threads WHERE id='t-old'")
        assert await execute("SELECT thread_id FROM jobs WHERE id='j-ok'") == [(None,)]
        await execute("DELETE FROM users WHERE id='u1'")
        assert await execute("SELECT count(*) FROM user_memory") == [(0,)]  # follows its user

        await asyncio.to_thread(command.downgrade, cfg, PREVIOUS)
        assert {"purpose", "connect_context", "updated_at"} <= await columns("chat_threads")
        assert await execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='user_memory'"
        ) == [(0,)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
