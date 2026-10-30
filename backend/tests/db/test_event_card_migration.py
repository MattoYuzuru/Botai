"""Upgrade/rollback of the event card, open_url and site map additions on a disposable database."""

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
BEFORE, THIS = "e4a91b7c603d", "b5d1c8e7a42f"


async def test_card_migration_keeps_existing_events_and_rolls_back(database_url: str) -> None:
    name = f"botai_card_migration_{uuid.uuid4().hex[:10]}"
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

    columns = (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='{table}' AND column_name='{column}'"
    )
    try:
        await asyncio.to_thread(command.upgrade, cfg, BEFORE)
        await execute(
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('card-user','card@fakeuni.test','Fixture','unusable-hash',false,false)"
        )
        await execute(
            "INSERT INTO spaces (id,user_id,title,accent_hue,time_zone,profile) "
            "VALUES ('card-space','card-user','Fixture',262.2,'Europe/Moscow','{}')"
        )
        await execute(
            "INSERT INTO connections (id,user_id,space_id,origin,kind,title,status,allow_hosts) "
            "VALUES ('card-source','card-user','card-space','https://fakeuni.test',"
            "'lms','Fixture','draft','[]')"
        )
        await execute(
            "INSERT INTO schedule_events (id,user_id,connection_id,external_id,source_url,"
            "fetched_at,seen_at,title,kind,starts_at,ends_at,format,changed) VALUES "
            "('card-event','card-user','card-source','ev','https://fakeuni.test/api',now(),now(),"
            "'Пара','other',now(),now(),'unknown',false)"
        )
        await asyncio.to_thread(command.upgrade, cfg, THIS)
        assert await execute(
            "SELECT title, open_url, kind_text, address, group_text, lesson_number, links, "
            "attributes FROM schedule_events"
        ) == [("Пара", None, None, None, None, None, [], [])]
        await execute(
            "INSERT INTO source_sitemaps (origin,map) VALUES ('https://fakeuni.test','{}')"
        )
        assert await execute("SELECT origin FROM source_sitemaps") == [("https://fakeuni.test",)]
        for table in ("courses", "news_items", "deadlines"):
            assert await execute(columns.format(table=table, column="open_url")) != []

        await asyncio.to_thread(command.downgrade, cfg, BEFORE)
        assert await execute("SELECT title FROM schedule_events") == [("Пара",)]
        assert await execute(columns.format(table="schedule_events", column="links")) == []
        assert await execute(columns.format(table="courses", column="open_url")) == []
        assert await execute("SELECT to_regclass('source_sitemaps')") == [(None,)]
        await asyncio.to_thread(command.upgrade, cfg, THIS)
        assert await execute("SELECT open_url FROM schedule_events") == [(None,)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
