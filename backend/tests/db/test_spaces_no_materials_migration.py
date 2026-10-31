"""The "My materials" removal migration on a disposable database: data kept, index dropped.

A space always has a university (docs/v4/03-sources-and-ux.md §1.6, FR-060). The revision drops the
partial unique index of the old per-user "materials" bucket and does not delete any row: a legacy
space without `university_id` stays as it was (D-016 allows recreating production, but data is not
lost without need). The downgrade recreates the index exactly, so it needs at most one such legacy
row per user — the test proves both directions.
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
BEFORE = "f2b7d9c4a6e8"

INSERT_SPACE = (
    "INSERT INTO spaces (id,user_id,university_id,title,accent_hue,time_zone,profile) "
    "VALUES (:id,:user,:university,:title,250,'Europe/Moscow','{}')"
)


async def test_materials_space_migration_round_trip(database_url: str) -> None:
    name = f"botai_spaces_no_materials_{uuid.uuid4().hex[:10]}"
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

    try:
        await asyncio.to_thread(command.upgrade, cfg, BEFORE)
        await execute(
            "INSERT INTO users (id,email,display_name,password_hash,is_owner,disabled) "
            "VALUES ('m-user','m@fakeuni.test','Fixture','x',false,false)"
        )
        await execute(
            "INSERT INTO universities (id,title,short_title,origins,accent_hue,accent_verified) "
            "VALUES ('m-uni','Вуз','В','[]',250,false)"
        )
        # the old bucket: one space without a university per user, enforced by the unique index
        await execute(
            INSERT_SPACE,
            id="m-legacy",
            user="m-user",
            university=None,
            title="Мои материалы",
        )
        with pytest.raises(Exception, match="uq_spaces"):
            await execute(
                INSERT_SPACE,
                id="m-second",
                user="m-user",
                university=None,
                title="Ещё материалы",
            )

        await asyncio.to_thread(command.upgrade, cfg, "head")

        # the legacy row is kept exactly as it was: nothing is deleted, nothing is guessed
        assert await execute("SELECT id, university_id, title FROM spaces ORDER BY id") == [
            ("m-legacy", None, "Мои материалы")
        ]
        # the old bucket is no longer a model concept: no unique index, the API hides the row
        assert (
            await execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'spaces' "
                "AND indexname = 'uq_spaces_user_id_materials'"
            )
            == []
        )
        await execute(
            INSERT_SPACE, id="m-second", user="m-user", university=None, title="Ещё материалы"
        )
        assert await execute("SELECT count(*) FROM spaces") == [(2,)]
        # the real rule stays: one space per university per user
        await execute(
            INSERT_SPACE, id="m-uni-space", user="m-user", university="m-uni", title="Вуз"
        )
        with pytest.raises(Exception, match="uq_spaces"):
            await execute(
                INSERT_SPACE, id="m-dup", user="m-user", university="m-uni", title="Дубль"
            )

        # the downgrade recreates the old rule: a second legacy row for one user cannot survive it
        await execute("DELETE FROM spaces WHERE id = 'm-second'")
        await asyncio.to_thread(command.downgrade, cfg, BEFORE)
        assert await execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'spaces' "
            "AND indexname = 'uq_spaces_user_id_materials'"
        ) == [("uq_spaces_user_id_materials",)]
        assert await execute("SELECT id FROM spaces ORDER BY id") == [
            ("m-legacy",),
            ("m-uni-space",),
        ]
        with pytest.raises(Exception, match="uq_spaces"):
            await execute(
                INSERT_SPACE,
                id="m-third",
                user="m-user",
                university=None,
                title="Ещё материалы",
            )
        await asyncio.to_thread(command.upgrade, cfg, "head")
        assert await execute("SELECT count(*) FROM spaces") == [(2,)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
