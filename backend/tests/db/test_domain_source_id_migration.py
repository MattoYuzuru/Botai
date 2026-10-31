"""Domain `source_id` migration on a disposable database: provenance, keys, rollback.

The upgrade adds a nullable `source_id` (FK to `sources`, ON DELETE SET NULL) and a partial unique
`(source_id, external_id)` to every v4-backed domain table, and makes `connection_id` nullable so
a v4 source can write its rows without a connection. Existing v3 rows keep their connection and
their natural key. The downgrade drops exactly that and refuses to keep v4-only rows it cannot
represent.
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
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.db
BACKEND = Path(__file__).resolve().parents[2]
BEFORE = "a8d4f1c2b6e9"
TABLES = (
    "courses",
    "schedule_events",
    "assignments",
    "deadlines",
    "grades",
    "people",
    "locations",
    "news_items",
)
INSERT_V3_EVENT = (
    "INSERT INTO schedule_events (id,user_id,connection_id,external_id,source_url,"
    "fetched_at,seen_at,title,kind,starts_at,ends_at,format,changed) VALUES "
    "(:id,'dom-user','dom-conn',:external,'https://fakeuni.test/api',now(),now(),"
    "'Пара','other',now(),now(),'unknown',false)"
)
INSERT_V4_EVENT = (
    "INSERT INTO schedule_events (id,user_id,source_id,external_id,source_url,"
    "fetched_at,seen_at,title,kind,starts_at,ends_at,format,changed) VALUES "
    "(:id,'dom-user',:source_id,:external,'https://fakeuni.test/api',now(),now(),"
    "'Пара','other',now(),now(),'unknown',false)"
)


async def test_domain_source_id_migration_round_trip(database_url: str) -> None:
    name = f"botai_domain_source_id_{uuid.uuid4().hex[:10]}"
    root_url, _, _ = database_url.rpartition("/")
    target_url = f"{root_url}/{name}"
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "src/botai/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", target_url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    assert head is not None
    assert head != BEFORE

    async def execute(sql: str, **params: object) -> list[tuple[object, ...]]:
        engine = create_async_engine(target_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql), params)
                return [tuple(row) for row in result] if result.returns_rows else []
        finally:
            await engine.dispose()

    async def fails(sql: str, **params: object) -> None:
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await execute(sql, **params)

    try:
        await asyncio.to_thread(command.upgrade, cfg, BEFORE)
        await execute(
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('dom-user','dom@fakeuni.test','Fixture','x',false,false)"
        )
        await execute(
            "INSERT INTO spaces (id,user_id,title,accent_hue,time_zone,profile) "
            "VALUES ('dom-space','dom-user','Fixture',262.2,'Europe/Moscow','{}')"
        )
        await execute(
            "INSERT INTO connections (id,user_id,space_id,origin,kind,title,status,allow_hosts) "
            "VALUES ('dom-conn','dom-user','dom-space','https://fakeuni.test','lms','ЛК','draft','[]')"
        )
        await execute(INSERT_V3_EVENT, id="v3-event", external="ev-1")

        await asyncio.to_thread(command.upgrade, cfg, "head")

        # The old row is untouched: same connection, no source, still exactly one.
        assert await execute(
            "SELECT connection_id, source_id, title FROM schedule_events WHERE id = 'v3-event'"
        ) == [("dom-conn", None, "Пара")]

        # Every domain table got the column; both provenance columns are nullable now.
        columns = [
            (row[0], row[1], row[2])
            for row in await execute(
                "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
                "WHERE column_name IN ('source_id','connection_id') "
                "AND table_name = ANY(:tables)",
                tables=list(TABLES),
            )
        ]
        assert {(table, "source_id") for table in TABLES} <= {(t, c) for t, c, _ in columns}
        for table, column, nullable in columns:
            assert nullable == "YES", (table, column)
        delete_rules = await execute(
            "SELECT confdeltype::text FROM pg_constraint "
            "WHERE conname = 'fk_schedule_events_source_id_sources'"
        )
        assert delete_rules == [("n",)]  # SET NULL: deleting a source keeps its data

        # Two v4 sources of one host coexist; the natural key is per source.
        for source_id, identity in (
            ("dom-src-1", "https://fakeuni.test/schedule"),
            ("dom-src-2", "https://fakeuni.test/calendar"),
        ):
            await execute(
                "INSERT INTO sources (id,user_id,space_id,title,identity_key) "
                "VALUES (:id,'dom-user','dom-space','Источник',:identity)",
                id=source_id,
                identity=identity,
            )
        await execute(INSERT_V4_EVENT, id="v4-event-1", source_id="dom-src-1", external="ev-1")
        await execute(INSERT_V4_EVENT, id="v4-event-2", source_id="dom-src-2", external="ev-1")
        await fails(
            "INSERT INTO schedule_events (id,user_id,connection_id,source_id,external_id,"
            "source_url,fetched_at,seen_at,title,starts_at,ends_at) VALUES "
            "('v4-event-dup','dom-user',NULL,'dom-src-1','ev-1','https://fakeuni.test/api',"
            "now(),now(),'Дубль',now(),now())"
        )
        # The v4 row with the same external id as the v3 row lives next to it: the two natural
        # keys are independent (`v3-event` and `v4-event-1` both exist).
        partial = await execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'schedule_events' "
            "AND indexname = 'uq_schedule_events_source_ext'"
        )
        assert len(partial) == 1
        assert "UNIQUE" in str(partial[0][0])
        assert "WHERE (source_id IS NOT NULL)" in str(partial[0][0])

        # A source is deleted: its data stays and loses only the pointer.
        await execute("DELETE FROM sources WHERE id = 'dom-src-2'")
        assert await execute("SELECT source_id FROM schedule_events WHERE id = 'v4-event-2'") == [
            (None,)
        ]
        assert await execute("SELECT count(*) FROM schedule_events") == [(3,)]

        # Rollback: v4-only rows go (they cannot exist without the column), v3 rows survive.
        await asyncio.to_thread(command.downgrade, cfg, BEFORE)
        assert await execute("SELECT id FROM schedule_events ORDER BY id") == [("v3-event",)]
        assert (
            await execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'schedule_events' AND column_name = 'source_id'"
            )
            == []
        )
        connection_nullable = await execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'schedule_events' AND column_name = 'connection_id'"
        )
        assert connection_nullable == [("NO",)]
        assert await execute("SELECT count(*) FROM users") == [(1,)]

        await asyncio.to_thread(command.upgrade, cfg, "head")
        assert await execute("SELECT count(*) FROM schedule_events") == [(1,)]
        assert await execute("SELECT version_num FROM alembic_version") == [(head,)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
