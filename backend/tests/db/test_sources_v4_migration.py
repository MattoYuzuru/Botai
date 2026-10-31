"""The sources-v4 migration on a disposable database: tables, indexes, constraints, rollback.

Additive: the upgrade must not touch v3 tables, and the downgrade must drop exactly the new ones.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.db
BACKEND = Path(__file__).resolve().parents[2]
BEFORE = "d9f3a1c7e582"

TABLES = (
    "sources",
    "playbooks",
    "playbook_versions",
    "source_artifacts",
    "source_state",
    "source_priorities",
)


async def test_sources_v4_migration_round_trip(database_url: str) -> None:
    name = f"botai_sources_v4_{uuid.uuid4().hex[:10]}"
    root_url, _, _ = database_url.rpartition("/")
    target_url = f"{root_url}/{name}"
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "src/botai/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", target_url)
    # The chain keeps growing: take the current head instead of pinning a revision id.
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
            "INSERT INTO universities (id,title,short_title,origins,accent_hue,accent_verified) "
            "VALUES ('hse','НИУ ВШЭ','ВШЭ','[]',250,false)"
        )
        await execute(
            "INSERT INTO spaces (id,user_id,university_id,title,accent_hue,time_zone,profile) "
            "VALUES ('s1','u1','hse','НИУ ВШЭ',250,'Europe/Moscow','{}')"
        )

        head = ScriptDirectory.from_config(cfg).get_current_head()
        assert head is not None
        await asyncio.to_thread(command.upgrade, cfg, "head")

        tables = {
            row[0]
            for row in await execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert set(TABLES) <= tables
        versions = await execute("SELECT version_num FROM alembic_version")
        assert versions == [(head,)]

        # Sources are unique per space by identity, not by origin; two paths of one host coexist.
        await execute(
            "INSERT INTO sources (id,user_id,space_id,title,identity_key) "
            "VALUES ('src-1','u1','s1','Расписание','https://lk.example.org/schedule')"
        )
        await execute(
            "INSERT INTO sources (id,user_id,space_id,title,identity_key) "
            "VALUES ('src-2','u1','s1','Задания','https://lk.example.org/tasks')"
        )
        await fails(
            "INSERT INTO sources (id,user_id,space_id,title,identity_key) "
            "VALUES ('src-3','u1','s1','Дубль','https://lk.example.org/schedule')"
        )
        await fails(
            "INSERT INTO sources (id,user_id,space_id,title,identity_key,status) "
            "VALUES ('src-4','u1','s1','Плохой статус','https://lk.example.org/y','teleported')"
        )
        await fails(
            "INSERT INTO sources (id,user_id,space_id,title,identity_key,trust) "
            "VALUES ('src-5','u1','s1','Плохое доверие','https://lk.example.org/z','absolute')"
        )
        identity_index = await execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'sources' "
            "AND indexname = 'uq_sources_space_identity_key'"
        )
        assert len(identity_index) == 1
        assert "UNIQUE" in str(identity_index[0][0])
        assert "WHERE (identity_key IS NOT NULL)" in str(identity_index[0][0])

        # A playbook has one active version at most; a version needs a spec or code.
        await execute(
            "INSERT INTO playbooks (id,owner_id,title,language) "
            "VALUES ('pb-1','u1','ЛК','declarative')"
        )
        await execute(
            "INSERT INTO playbook_versions (id,playbook_id,version,spec,active) "
            "VALUES ('pv-1','pb-1',1,'{\"reads\": []}'::jsonb,true)"
        )
        await fails(
            "INSERT INTO playbook_versions (id,playbook_id,version,spec,active) "
            "VALUES ('pv-2','pb-1',2,'{\"reads\": []}'::jsonb,true)"
        )
        await fails(
            "INSERT INTO playbook_versions (id,playbook_id,version,active) "
            "VALUES ('pv-3','pb-1',3,false)"
        )
        await execute(
            "INSERT INTO playbook_versions (id,playbook_id,version,code,active) "
            "VALUES ('pv-4','pb-1',4,'print(1)',false)"
        )

        # Artifacts and state follow their source; priorities follow the space.
        await execute(
            "INSERT INTO source_artifacts (id,user_id,source_id,kind,sha256) "
            "VALUES ('art-1','u1','src-1','page',repeat('a', 64))"
        )
        await execute(
            "INSERT INTO source_state (source_id,last_revision,cursor) "
            "VALUES ('src-1',2,'{\"page\": 2}'::jsonb)"
        )
        await execute(
            "INSERT INTO source_priorities (space_id,kind,source_ids) "
            "VALUES ('s1','schedule','[\"src-1\", \"src-2\"]'::jsonb)"
        )
        await fails(
            "INSERT INTO source_artifacts (id,user_id,source_id,kind,sha256) "
            "VALUES ('art-2','u1','src-1','video',repeat('a', 64))"
        )
        await execute("DELETE FROM spaces WHERE id = 's1'")
        assert await execute("SELECT count(*) FROM sources") == [(0,)]
        assert await execute("SELECT count(*) FROM source_artifacts") == [(0,)]
        assert await execute("SELECT count(*) FROM source_state") == [(0,)]
        assert await execute("SELECT count(*) FROM source_priorities") == [(0,)]

        # Rollback drops only the new tables; v3 data was not touched by the upgrade.
        await asyncio.to_thread(command.downgrade, cfg, BEFORE)
        left = {
            row[0]
            for row in await execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        assert not (set(TABLES) & left)
        assert await execute("SELECT count(*) FROM users") == [(1,)]
        assert await execute("SELECT count(*) FROM spaces") == [(0,)]

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


async def test_sources_v4_columns_match_the_service(engine: AsyncEngine) -> None:
    """Spot-check the columns the service reads and writes (the full diff is test_schema)."""

    def columns(sync: Connection) -> dict[str, set[str]]:
        inspector = inspect(sync)
        return {
            table: {column["name"] for column in inspector.get_columns(table)} for table in TABLES
        }

    async with engine.connect() as conn:
        found = await conn.run_sync(columns)
    assert {"identity_key", "kinds", "status", "playbook_id", "trust"} <= found["sources"]
    assert {"current_version", "language", "health", "provenance"} <= found["playbooks"]
    assert {"version", "spec", "code", "tests", "canary", "active", "checksum"} <= found[
        "playbook_versions"
    ]
    assert {"kind", "url", "sha256", "content_type", "size", "storage_ref"} <= found[
        "source_artifacts"
    ]
    assert {"last_revision", "cursor", "site_map", "endpoints"} <= found["source_state"]
    assert {"source_ids"} <= found["source_priorities"]
