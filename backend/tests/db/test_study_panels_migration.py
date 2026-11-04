"""Panels migration on a disposable database: additive upgrade, constraints, exact rollback.

The upgrade adds `panels`, `profile_fields`, `ranking_entries` and two nullable columns of
`grades`; nothing is rewritten. The downgrade drops exactly those and leaves every other row.
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
REVISION = "d5b2e7f9a1c3"
NEW_TABLES = ("panels", "profile_fields", "ranking_entries")

INSERT_PANEL = (
    "INSERT INTO panels (id,user_id,space_id,key,type,spec,origin) "
    "VALUES (:id,'pan-user','pan-space',:key,:type,'{}'::jsonb,:origin)"
)


async def test_study_panels_migration_round_trip(database_url: str) -> None:
    name = f"botai_panels_{uuid.uuid4().hex[:10]}"
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
    before = str(revision.down_revision)

    async def execute(sql: str, **params: object) -> list[tuple[object, ...]]:
        engine = create_async_engine(target_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql), params)
                return [tuple(row) for row in result] if result.returns_rows else []
        finally:
            await engine.dispose()

    async def fails(sql: str, **params: object) -> None:
        with pytest.raises(IntegrityError):
            await execute(sql, **params)

    async def tables() -> set[str]:
        rows = await execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        return {str(row[0]) for row in rows}

    try:
        await asyncio.to_thread(command.upgrade, cfg, before)
        assert not set(NEW_TABLES) & await tables()
        await execute(
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('pan-user','pan@fakeuni.test','Fixture','x',false,false)"
        )
        await execute(
            "INSERT INTO spaces (id,user_id,title,accent_hue,time_zone,profile) "
            "VALUES ('pan-space','pan-user','Fixture',262.2,'Europe/Moscow','{}')"
        )
        await execute(
            "INSERT INTO connections (id,user_id,space_id,origin,kind,title,status,allow_hosts) "
            "VALUES ('pan-conn','pan-user','pan-space','https://fakeuni.test','lms','ЛК','draft','[]')"
        )
        await execute(
            "INSERT INTO grades (id,user_id,connection_id,external_id,source_url,fetched_at,"
            "seen_at,value,scale) VALUES ('old-grade','pan-user','pan-conn','g-1',"
            "'https://fakeuni.test/api',now(),now(),'8','10')"
        )

        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        assert set(NEW_TABLES) <= await tables()

        # the old grade is exactly as it was, with the two new columns empty
        assert await execute(
            "SELECT value, scale, value_num, scale_max FROM grades WHERE id = 'old-grade'"
        ) == [("8", "10", None, None)]
        await execute("UPDATE grades SET value_num = 8, scale_max = 10 WHERE id = 'old-grade'")
        assert await execute("SELECT value_num, scale_max FROM grades") == [(8.0, 10.0)]

        # panels: closed vocabularies, a key per space, cascades
        await execute(INSERT_PANEL, id="p-1", key="grades-overview", type="kpi", origin="system")
        await fails(INSERT_PANEL, id="p-2", key="grades-overview", type="chart", origin="system")
        await fails(INSERT_PANEL, id="p-3", key="x", type="gauge", origin="system")
        await fails(INSERT_PANEL, id="p-4", key="x", type="kpi", origin="human")
        await fails(
            "INSERT INTO panels (id,user_id,space_id,key,type,spec,origin,status) "
            "VALUES ('p-5','pan-user','pan-space','y','kpi','{}'::jsonb,'agent','deleted')"
        )
        defaults = await execute(
            "SELECT status, visible, hidden_in_all, position, source_id, proposed_by_job_id "
            "FROM panels WHERE id = 'p-1'"
        )
        assert defaults == [("active", True, False, 0, None, None)]

        # a source: panels and domain rows point at it with SET NULL
        await execute(
            "INSERT INTO sources (id,user_id,space_id,title,identity_key) "
            "VALUES ('pan-src','pan-user','pan-space','Источник','https://fakeuni.test/app')"
        )
        await execute("UPDATE panels SET source_id = 'pan-src' WHERE id = 'p-1'")
        await execute(
            "INSERT INTO profile_fields (id,user_id,source_id,external_id,source_url,fetched_at,"
            "seen_at,field,label,value) VALUES ('pf-1','pan-user','pan-src','group',"
            "'https://fakeuni.test/api',now(),now(),'group','Группа','ПМ-31')"
        )
        await execute(
            "INSERT INTO ranking_entries (id,user_id,source_id,external_id,source_url,fetched_at,"
            "seen_at,board,position,label) VALUES ('re-1','pan-user','pan-src','flow-1',"
            "'https://fakeuni.test/api',now(),now(),'Поток',1,'Иванова А.')"
        )
        assert await execute("SELECT is_me, score_num, total FROM ranking_entries") == [
            (False, None, None)
        ]
        # a second row with the same external id under the same source is a duplicate
        await fails(
            "INSERT INTO profile_fields (id,user_id,source_id,external_id,source_url,fetched_at,"
            "seen_at,field,label,value) VALUES ('pf-2','pan-user','pan-src','group',"
            "'https://fakeuni.test/api',now(),now(),'group','Группа','ПМ-32')"
        )
        await execute("DELETE FROM sources WHERE id = 'pan-src'")
        assert await execute("SELECT source_id FROM panels WHERE id = 'p-1'") == [(None,)]
        assert await execute("SELECT source_id FROM profile_fields") == [(None,)]

        # a space takes its panels with it
        await execute(
            "INSERT INTO spaces (id,user_id,title,accent_hue,time_zone,profile) "
            "VALUES ('pan-space-2','pan-user','Второй',262.2,'Europe/Moscow','{}')"
        )
        await execute(
            "INSERT INTO panels (id,user_id,space_id,key,type,spec,origin) "
            "VALUES ('p-6','pan-user','pan-space-2','second','list','{}'::jsonb,'agent')"
        )
        await execute("DELETE FROM spaces WHERE id = 'pan-space-2'")
        assert await execute("SELECT id FROM panels") == [("p-1",)]

        # rollback: exactly the new tables and columns go; the old grade and the user stay
        await asyncio.to_thread(command.downgrade, cfg, before)
        assert not set(NEW_TABLES) & await tables()
        columns = await execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'grades'"
        )
        assert {"value_num", "scale_max"}.isdisjoint({str(c[0]) for c in columns})
        assert await execute("SELECT value, scale FROM grades WHERE id = 'old-grade'") == [
            ("8", "10")
        ]
        assert await execute("SELECT count(*) FROM users") == [(1,)]

        # and it applies again on top of the kept data
        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        assert await execute(
            "SELECT value, value_num, scale_max FROM grades WHERE id = 'old-grade'"
        ) == [("8", None, None)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
