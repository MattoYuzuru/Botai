"""The source-planning migration on a disposable database: column, index, backfill, rollback.

Additive: `sources.next_sync_at` appears (nullable timestamptz + index) and only rows that can be
synced in the background get a first planned time; everything else stays NULL (manual only). The
downgrade removes exactly the index and the column; the existing source rows are not touched.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.db
BACKEND = Path(__file__).resolve().parents[2]
BEFORE = "a9c4e2f7b1d6"
REVISION = "b4e8c1a7d920"
INTERVAL = 21_600


def _fixture_statements() -> list[str]:
    """A v3/v4 mixed world before the planning column exists: every status the backfill cares about."""
    return [
        "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
        "VALUES ('01M68Z000000000000000000U1','u1@fakeuni.test','Fixture','x',false,false)",
        "INSERT INTO universities (id,title,short_title,origins,accent_hue,accent_verified) "
        "VALUES ('uni-fixture','Fixture','Fixture','[]'::jsonb,262.2,false)",
        "INSERT INTO spaces (id,user_id,university_id,title,accent_hue,time_zone,profile) "
        "VALUES ('01M68Z000000000000000000S1','01M68Z000000000000000000U1','uni-fixture',"
        "'Fixture',262.2,'Europe/Moscow','{}'::jsonb)",
        "INSERT INTO playbooks (id,university_id,owner_id,title,language,current_version,health,"
        "provenance) VALUES ('01M68Z000000000000000000P1','uni-fixture',NULL,'Fixture',"
        "'declarative',1,'{}'::jsonb,'{}'::jsonb)",
        "INSERT INTO sources (id,user_id,space_id,university_id,title,identity_key,kinds,status,"
        "playbook_id,config,priority,trust) VALUES "
        "('01M68Z000000000000000001A1','01M68Z000000000000000000U1',"
        "'01M68Z000000000000000000S1','uni-fixture','Active reader',"
        "'https://reader.fakeuni.test','[]'::jsonb,'active',"
        "'01M68Z000000000000000000P1','{}'::jsonb,0,'normal'),"
        "('01M68Z000000000000000001A2','01M68Z000000000000000000U1',"
        "'01M68Z000000000000000000S1','uni-fixture','Stale reader',"
        "'https://stale.fakeuni.test','[]'::jsonb,'stale',"
        "'01M68Z000000000000000000P1','{}'::jsonb,0,'normal'),"
        "('01M68Z000000000000000001A3','01M68Z000000000000000000U1',"
        "'01M68Z000000000000000000S1','uni-fixture','Unfinished setup',"
        "'https://draft.fakeuni.test','[]'::jsonb,'draft',"
        "'01M68Z000000000000000000P1','{}'::jsonb,0,'normal'),"
        "('01M68Z000000000000000001A4','01M68Z000000000000000000U1',"
        "'01M68Z000000000000000000S1','uni-fixture','No playbook',"
        "'https://manual.fakeuni.test','[]'::jsonb,'active',"
        "NULL,'{}'::jsonb,0,'normal'),"
        "('01M68Z000000000000000001A5','01M68Z000000000000000000U1',"
        "'01M68Z000000000000000000S1','uni-fixture','Switched off',"
        "'https://off.fakeuni.test','[]'::jsonb,'disabled',"
        "'01M68Z000000000000000000P1','{}'::jsonb,0,'normal'),"
        "('01M68Z000000000000000001A6','01M68Z000000000000000000U1',"
        "'01M68Z000000000000000000S1','uni-fixture','Needs login',"
        "'https://auth.fakeuni.test','[]'::jsonb,'needs_reauth',"
        "'01M68Z000000000000000000P1','{}'::jsonb,0,'normal')",
    ]


async def test_source_next_sync_migration_round_trip(database_url: str) -> None:
    name = f"botai_source_sync_{uuid.uuid4().hex[:10]}"
    root_url, _, _ = database_url.rpartition("/")
    target_url = f"{root_url}/{name}"
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "src/botai/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", target_url)
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    assert head is not None
    assert script.get_heads() == [head], "the migration chain must have exactly one head"
    assert REVISION in {rev.revision for rev in script.walk_revisions()}

    async def execute(sql: str) -> list[tuple[object, ...]]:
        engine = create_async_engine(target_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql))
                return [tuple(row) for row in result] if result.returns_rows else []
        finally:
            await engine.dispose()

    try:
        await asyncio.to_thread(command.upgrade, cfg, BEFORE)
        for statement in _fixture_statements():
            await execute(statement)

        await asyncio.to_thread(command.upgrade, cfg, "head")

        columns = {
            row[0]
            for row in await execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'sources'"
            )
        }
        assert "next_sync_at" in columns
        indexes = {
            row[0]
            for row in await execute("SELECT indexname FROM pg_indexes WHERE tablename = 'sources'")
        }
        assert "ix_sources_next_sync_at" in indexes
        versions = await execute("SELECT version_num FROM alembic_version")
        assert versions == [(head,)]

        rows = dict(
            (row[0], row[1])
            for row in await execute("SELECT id, next_sync_at FROM sources ORDER BY id")
        )
        now = datetime.now(UTC)
        # Syncable sources are planned within the next 6–12 hours (interval + deterministic jitter).
        for source_id in (
            "01M68Z000000000000000001A1",
            "01M68Z000000000000000001A2",
            "01M68Z000000000000000001A6",
        ):
            planned = rows[source_id]
            assert planned is not None, source_id
            assert (
                now + timedelta(seconds=INTERVAL) - timedelta(minutes=5)
                <= planned
                <= now + timedelta(seconds=2 * INTERVAL) + timedelta(minutes=5)
            ), (source_id, planned)
        # Not planned: unfinished setup, no playbook, switched off — a manual sync stays the only way.
        for source_id in (
            "01M68Z000000000000000001A3",
            "01M68Z000000000000000001A4",
            "01M68Z000000000000000001A5",
        ):
            assert rows[source_id] is None, source_id
        # The backfill only adds a planning time: the source rows themselves are untouched.
        titles = dict((row[0], row[1]) for row in await execute("SELECT id, title FROM sources"))
        assert titles["01M68Z000000000000000001A1"] == "Active reader"

        await asyncio.to_thread(command.downgrade, cfg, BEFORE)
        columns = {
            row[0]
            for row in await execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'sources'"
            )
        }
        assert "next_sync_at" not in columns
        assert await execute("SELECT count(*) FROM sources") == [(6,)]

        await asyncio.to_thread(command.upgrade, cfg, "head")
        assert await execute("SELECT count(*) FROM sources") == [(6,)]
        versions = await execute("SELECT version_num FROM alembic_version")
        assert versions == [(head,)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
