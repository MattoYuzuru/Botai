from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

BACKEND = Path(__file__).resolve().parents[2]


def test_single_alembic_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "src/botai/db/migrations"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"several alembic heads, rebase your migration: {heads}"


@pytest.mark.db
async def test_migrations_create_all_tables(engine: AsyncEngine) -> None:
    from botai.db import models  # noqa: F401
    from botai.db.base import Base

    async with engine.connect() as conn:
        rows = await conn.execute(text("select tablename from pg_tables where schemaname='public'"))
        tables = {r[0] for r in rows}
    missing = set(Base.metadata.tables) - tables
    assert not missing, missing


@pytest.mark.db
async def test_chunk_tsvector_is_russian(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        value = (
            await conn.execute(text("select to_tsvector('russian', 'расписания пар')"))
        ).scalar_one()
    assert "расписан" in str(value)
