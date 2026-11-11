"""Grades layers / status rules migration on a disposable database: additive, exact rollback.

The upgrade adds nullable columns to `grades` and `deadlines`, `user_memory.kind` with a constant
default and the `status_rules` table; old rows keep their values. The downgrade drops exactly
those and leaves every other row.
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
REVISION = "f7c2d8e4a5b6"
GRADE_COLUMNS = {"layer", "period", "assignment_external_id", "url", "comment", "changed_at"}
DEADLINE_COLUMNS = {"state_text", "user_state", "changed_at"}

INSERT_RULE = (
    "INSERT INTO status_rules (id,user_id,source_id,label,state) "
    "VALUES (:id,'gl-user',:source_id,:label,:state)"
)


async def test_grades_layers_migration_round_trip(database_url: str) -> None:
    name = f"botai_layers_{uuid.uuid4().hex[:10]}"
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

    async def columns(table: str) -> set[str]:
        rows = await execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t", t=table
        )
        return {str(row[0]) for row in rows}

    try:
        await asyncio.to_thread(command.upgrade, cfg, before)
        assert not GRADE_COLUMNS & await columns("grades")
        await execute(
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('gl-user','gl@fakeuni.test','Fixture','x',false,false)"
        )
        await execute(
            "INSERT INTO spaces (id,user_id,title,accent_hue,time_zone,profile) "
            "VALUES ('gl-space','gl-user','Fixture',262.2,'Europe/Moscow','{}')"
        )
        await execute(
            "INSERT INTO sources (id,user_id,space_id,title,identity_key) "
            "VALUES ('gl-src','gl-user','gl-space','Источник','https://fakeuni.test/app')"
        )
        await execute(
            "INSERT INTO grades (id,user_id,source_id,external_id,source_url,fetched_at,"
            "seen_at,value,scale) VALUES ('old-grade','gl-user','gl-src','g-1',"
            "'https://fakeuni.test/api',now(),now(),'8','10')"
        )
        await execute(
            "INSERT INTO deadlines (id,user_id,source_id,external_id,source_url,fetched_at,"
            "seen_at,title,state) VALUES ('old-dl','gl-user','gl-src','d-1',"
            "'https://fakeuni.test/api',now(),now(),'ДЗ 1','unknown')"
        )
        await execute("INSERT INTO user_memory (id,user_id,note) VALUES ('m-1','gl-user','Группа')")

        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        assert await columns("grades") >= GRADE_COLUMNS
        assert await columns("deadlines") >= DEADLINE_COLUMNS

        # old rows are exactly as they were, the new columns empty, memory became a note
        assert await execute(
            "SELECT value, scale, layer, period, assignment_external_id, url, comment, changed_at "
            "FROM grades WHERE id = 'old-grade'"
        ) == [("8", "10", None, None, None, None, None, None)]
        assert await execute("SELECT state, state_text, user_state, changed_at FROM deadlines") == [
            ("unknown", None, None, None)
        ]
        assert await execute("SELECT note, kind FROM user_memory") == [("Группа", "note")]
        await execute(
            "INSERT INTO user_memory (id,user_id,note,kind) VALUES ('m-2','gl-user','R','rule')"
        )
        await fails(
            "INSERT INTO user_memory (id,user_id,note,kind) VALUES ('m-3','gl-user','X','fact')"
        )

        # status rules: closed states; one rule per (user, source, label) and per (user, label)
        # for the rules of every source (NULL source_id)
        await execute(
            INSERT_RULE, id="r-1", source_id="gl-src", label="отправлено", state="submitted"
        )
        await fails(INSERT_RULE, id="r-2", source_id="gl-src", label="отправлено", state="graded")
        await execute(INSERT_RULE, id="r-3", source_id=None, label="отправлено", state="submitted")
        await fails(INSERT_RULE, id="r-4", source_id=None, label="отправлено", state="open")
        await fails(INSERT_RULE, id="r-5", source_id=None, label="x", state="unknown")
        assert await execute("SELECT created_at IS NOT NULL FROM status_rules WHERE id='r-1'") == [
            (True,)
        ]
        # a deleted source takes its rules with it; the all-sources rule stays
        await execute("DELETE FROM sources WHERE id = 'gl-src'")
        assert await execute("SELECT id FROM status_rules") == [("r-3",)]

        # rollback: exactly the new columns and the table go; old rows and the user stay
        await asyncio.to_thread(command.downgrade, cfg, before)
        assert not GRADE_COLUMNS & await columns("grades")
        assert not DEADLINE_COLUMNS & await columns("deadlines")
        assert "kind" not in await columns("user_memory")
        assert not await columns("status_rules")
        assert await execute("SELECT value, scale FROM grades WHERE id = 'old-grade'") == [
            ("8", "10")
        ]
        assert await execute("SELECT count(*) FROM user_memory") == [(2,)]

        # and it applies again on top of the kept data
        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        assert await execute("SELECT kind FROM user_memory ORDER BY id") == [("note",), ("note",)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
