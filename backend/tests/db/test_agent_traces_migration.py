"""The agent-traces migration on a disposable database: tables, indexes, FKs, rollback.

Additive: the upgrade must not touch v3 tables, and the downgrade drops exactly the two new ones.
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
BEFORE = "b7e2c4a91d3f"
TABLES = ("agent_runs", "agent_run_events")


async def test_agent_traces_migration_round_trip(database_url: str) -> None:
    name = f"botai_agent_traces_{uuid.uuid4().hex[:10]}"
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
        with pytest.raises(IntegrityError):
            await execute(sql, **params)

    try:
        await asyncio.to_thread(command.upgrade, cfg, BEFORE)
        await execute(
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('u1','u1@fakeuni.test','Fixture','x',false,false)"
        )
        await execute(
            "INSERT INTO jobs (id,user_id,kind,state,payload,run_after,attempts) "
            "VALUES ('j1','u1','agent','running','{}'::jsonb,now(),0)"
        )

        await asyncio.to_thread(command.upgrade, cfg, "head")

        tables = {
            row[0]
            for row in await execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert set(TABLES) <= tables
        versions = await execute("SELECT version_num FROM alembic_version")
        assert versions == [(head,)]
        indexes = {
            row[0]
            for row in await execute(
                "SELECT indexname FROM pg_indexes WHERE tablename IN "
                "('agent_runs','agent_run_events')"
            )
        }
        assert {
            "ix_agent_runs_user_id",
            "ix_agent_runs_user_started",
            "ix_agent_runs_job",
            "ix_agent_run_events_run_seq",
            "uq_agent_run_events_run_seq",
        } <= indexes

        await execute(
            "INSERT INTO agent_runs (run_id,user_id,job_id,status) "
            "VALUES ('r1','u1','j1','running')"
        )
        await execute(
            "INSERT INTO agent_run_events (run_id,seq,kind,at,payload) "
            "VALUES ('r1',0,'run_started',now(),'{}'::jsonb), "
            "('r1',1,'run_finished',now(),'{\"status\": \"answer\"}'::jsonb)"
        )
        # One event per (run, seq): a replayed append is a conflict, never a duplicate row.
        await fails(
            "INSERT INTO agent_run_events (run_id,seq,kind,at) VALUES ('r1',1,'model_call',now())"
        )
        # The trace outlives the queue row: deleting the job nulls the link, keeps the trace.
        await execute("DELETE FROM jobs WHERE id = 'j1'")
        assert await execute("SELECT job_id FROM agent_runs WHERE run_id = 'r1'") == [(None,)]
        assert await execute("SELECT count(*) FROM agent_run_events") == [(2,)]
        # User deletion removes the traces (owner data).
        await execute("DELETE FROM users WHERE id = 'u1'")
        assert await execute("SELECT count(*) FROM agent_runs") == [(0,)]
        assert await execute("SELECT count(*) FROM agent_run_events") == [(0,)]

        # Rollback drops only the new tables; v3 tables stay.
        await asyncio.to_thread(command.downgrade, cfg, BEFORE)
        left = {
            row[0]
            for row in await execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert not (set(TABLES) & left)
        assert "jobs" in left
        assert "users" in left

        await asyncio.to_thread(command.upgrade, cfg, "head")
        back = {
            row[0]
            for row in await execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert set(TABLES) <= back
        assert await execute("SELECT version_num FROM alembic_version") == [(head,)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
