"""Additive checkpoint upgrade and rollback preserve old data on a disposable database."""

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
PREVIOUS = "9c2a7184be6d"
REVISION = "e4a91b7c603d"


async def test_checkpoint_migration_preserves_existing_rows_and_can_roll_back(
    database_url: str,
) -> None:
    name = f"botai_research_migration_{uuid.uuid4().hex[:10]}"
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

    async def existing_rows() -> list[list[tuple[object, ...]]]:
        return [
            await execute("SELECT id,display_name FROM users"),
            await execute("SELECT id,title,data_scope FROM connections"),
            await execute("SELECT id,state,payload,attempts FROM jobs"),
            await execute("SELECT kind,encode(ciphertext,'hex'),aad_version FROM secrets"),
        ]

    try:
        await asyncio.to_thread(command.upgrade, cfg, PREVIOUS)
        for sql in (
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('research-user','migration@fakeuni.test','Fixture','unusable-hash',false,false)",
            "INSERT INTO spaces (id,user_id,title,accent_hue,time_zone,profile) "
            "VALUES ('research-space','research-user','Fixture',262.2,'Europe/Moscow','{}')",
            "INSERT INTO connections (id,user_id,space_id,origin,kind,title,status,allow_hosts,data_scope) "
            "VALUES ('research-source','research-user','research-space','https://fakeuni.test',"
            "'lms','Fixture','active','[]','[\"scheduleEvents\"]')",
            "INSERT INTO jobs (id,user_id,connection_id,kind,state,payload,attempts,run_after) "
            "VALUES ('research-job','research-user','research-source','connect','queued','{}',3,now())",
            "INSERT INTO secrets (id,user_id,connection_id,kind,kek_id,wrapped_dek,nonce,ciphertext,aad_version) "
            "VALUES ('research-secret','research-user','research-source','session','test',"
            "decode('aabb','hex'),decode('ccdd','hex'),decode('1234','hex'),1)",
        ):
            await execute(sql)
        before = await existing_rows()
        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        assert await existing_rows() == before
        assert await execute("SELECT count(*) FROM job_research_checkpoints") == [(0,)]
        await execute(
            "INSERT INTO job_research_checkpoints "
            "(job_id,user_id,connection_id,revision,format_version,intent_key,updated_at,expires_at,"
            "kek_id,wrapped_dek,nonce,ciphertext,aad_version) VALUES "
            "('research-job','research-user','research-source',1,1,repeat('a',64),now(),"
            "now()+interval '24 hours','test',decode('aa','hex'),decode('bb','hex'),decode('cc','hex'),1)"
        )
        assert await execute("SELECT revision,format_version FROM job_research_checkpoints") == [
            (1, 1)
        ]
        await asyncio.to_thread(command.downgrade, cfg, PREVIOUS)
        assert await existing_rows() == before
        assert await execute("SELECT to_regclass('job_research_checkpoints')") == [(None,)]
        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        assert await existing_rows() == before
        assert await execute("SELECT count(*) FROM job_research_checkpoints") == [(0,)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
