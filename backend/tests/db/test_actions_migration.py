"""Actions policy/journal migration on a disposable database: tables, keys, cascade, rollback.

`action_policies` and `actions_log` are additive (the v3 action tables stay untouched). The
upgrade creates them with their partial unique indexes; the downgrade drops exactly them. The
test also proves the two guarantees the engine relies on: one global row per class, one journal
row per user idempotency key, and a user delete cascades to both tables.
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
BEFORE = "c3f7a2b9d4e1"
REVISION = "d6b8f2a14c73"

INSERT_USER = (
    "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
    "VALUES (:id,:email,:name,'x',false,false)"
)
INSERT_GLOBAL_POLICY = (
    "INSERT INTO action_policies (id,user_id,scope_kind,scope_id,action_class,mode) "
    "VALUES (:id,:user_id,'global',NULL,:class,:mode)"
)
INSERT_SCOPED_POLICY = (
    "INSERT INTO action_policies (id,user_id,scope_kind,scope_id,action_class,mode) "
    "VALUES (:id,:user_id,'site','fakeuni.test',:class,:mode)"
)
INSERT_LOG = (
    "INSERT INTO actions_log (user_id,run_id,action_class,mode,decision,intent,"
    "idempotency_key) VALUES (:user_id,'run-1','write_soft','auto','executed','intent',:key)"
)


async def test_actions_migration_round_trip(database_url: str) -> None:
    name = f"botai_actions_{uuid.uuid4().hex[:10]}"
    root_url, _, _ = database_url.rpartition("/")
    target_url = f"{root_url}/{name}"
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "src/botai/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", target_url)
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, heads  # a linear chain: other lanes may chain after this revision
    assert REVISION in {revision.revision for revision in script.walk_revisions()}

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
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename IN ('action_policies','actions_log')"
        )
        return {str(row[0]) for row in rows}

    try:
        await asyncio.to_thread(command.upgrade, cfg, BEFORE)
        assert await tables() == set()
        await asyncio.to_thread(command.upgrade, cfg, "head")
        assert await tables() == {"action_policies", "actions_log"}

        columns = {
            str(row[0])
            for row in await execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='actions_log'"
            )
        }
        assert {
            "proposal",
            "request",
            "result",
            "idempotency_key",
            "rollback",
            "run_id",
            "source_id",
        } <= columns

        await execute(INSERT_USER, id="act-user", email="act@example.test", name="Action")
        await execute(
            INSERT_GLOBAL_POLICY,
            id="01M68Z00000000000000000P01",
            user_id="act-user",
            **{"class": "write_soft"},
            mode="auto",
        )
        await execute(
            INSERT_SCOPED_POLICY,
            id="01M68Z00000000000000000P02",
            user_id="act-user",
            **{"class": "write_soft"},
            mode="never",
        )
        # One row per class and scope: a second global row of the same class collides; a scoped
        # row does not (different partial index), and another class is a different row.
        await fails(
            INSERT_GLOBAL_POLICY,
            id="01M68Z00000000000000000P03",
            user_id="act-user",
            **{"class": "write_soft"},
            mode="ask",
        )
        await fails(
            INSERT_GLOBAL_POLICY,
            id="01M68Z00000000000000000P04",
            user_id="act-user",
            **{"class": "read"},
            mode="maybe",
        )

        await execute(INSERT_LOG, user_id="act-user", key="key-1")
        await fails(INSERT_LOG, user_id="act-user", key="key-1")
        await execute(INSERT_LOG, user_id="act-user", key=None)  # NULL keys do not collide
        await execute(INSERT_LOG, user_id="act-user", key=None)

        await execute("DELETE FROM users WHERE id='act-user'")
        assert await execute("SELECT count(*) FROM action_policies WHERE user_id='act-user'") == [
            (0,)
        ]
        assert await execute("SELECT count(*) FROM actions_log WHERE user_id='act-user'") == [(0,)]

        await asyncio.to_thread(command.downgrade, cfg, BEFORE)
        assert await tables() == set()
        await asyncio.to_thread(command.upgrade, cfg, "head")
        assert await tables() == {"action_policies", "actions_log"}
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
