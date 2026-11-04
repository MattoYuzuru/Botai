"""The checkpoint migration on a disposable database: table, indexes, cascades, rollback (D-040).

Additive: one new table. A thread takes its checkpoints with it, a purged job only loses the link,
and the downgrade drops exactly that table.
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
BEFORE = "b4e8c1a7d920"
REVISION = "c3a1f0d2e4b5"
TABLE = "agent_run_checkpoints"


async def test_agent_checkpoints_migration_round_trip(database_url: str) -> None:
    name = f"botai_agent_checkpoints_{uuid.uuid4().hex[:10]}"
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

    try:
        await asyncio.to_thread(command.upgrade, cfg, BEFORE)
        assert TABLE not in {
            row[0]
            for row in await execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        await execute(
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('u1','u1@fakeuni.test','Fixture','x',false,false)"
        )
        await execute(
            "INSERT INTO chat_threads (id,user_id,title,last_message_at) "
            "VALUES ('t1','u1','Чат',now())"
        )
        await execute(
            "INSERT INTO jobs (id,user_id,thread_id,kind,state,payload,run_after,attempts) "
            "VALUES ('j1','u1','t1','chat','running','{}'::jsonb,now(),1)"
        )

        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        indexes = {
            row[0]
            for row in await execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'agent_run_checkpoints'"
            )
        }
        assert {
            "ix_agent_run_checkpoints_user_id",
            "ix_agent_run_checkpoints_thread",
            "ix_agent_run_checkpoints_job",
            "ix_agent_run_checkpoints_expires",
        } <= indexes

        await execute(
            "INSERT INTO agent_run_checkpoints (run_id,user_id,thread_id,job_id,chain_id,segment,status,"
            "resumable,messages,message_count,bytes,format_version,cost_usd_chain,tokens_chain,"
            "continuations,chain_started_at,updated_at,expires_at) "
            "VALUES ('r1','u1','t1','j1','j1',0,'step_limit',true,'[]'::jsonb,0,2,1,0,0,0,"
            "now(),now(),now() + interval '72 hours')"
        )
        # The queue row is short-lived: purging the job only drops the link.
        await execute("DELETE FROM jobs WHERE id = 'j1'")
        assert await execute("SELECT job_id, chain_id FROM agent_run_checkpoints") == [(None, "j1")]
        # A deleted thread takes its checkpoints with it.
        await execute("DELETE FROM chat_threads WHERE id = 't1'")
        assert await execute("SELECT count(*) FROM agent_run_checkpoints") == [(0,)]

        await execute(
            "INSERT INTO chat_threads (id,user_id,title,last_message_at) "
            "VALUES ('t2','u1','Чат',now())"
        )
        await execute(
            "INSERT INTO agent_run_checkpoints (run_id,user_id,thread_id,chain_id,segment,status,resumable,"
            "messages,message_count,bytes,format_version,cost_usd_chain,tokens_chain,"
            "continuations,chain_started_at,updated_at,expires_at) "
            "VALUES ('r2','u1','t2','j2',0,'error',true,'[]'::jsonb,0,2,1,0,0,0,"
            "now(),now(),now())"
        )
        # A deleted user takes everything (owner data).
        await execute("DELETE FROM users WHERE id = 'u1'")
        assert await execute("SELECT count(*) FROM agent_run_checkpoints") == [(0,)]

        await asyncio.to_thread(command.downgrade, cfg, BEFORE)
        left = {
            row[0]
            for row in await execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert TABLE not in left
        assert {"jobs", "users", "chat_threads", "agent_runs"} <= left
        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        assert await execute("SELECT version_num FROM alembic_version") == [(REVISION,)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
