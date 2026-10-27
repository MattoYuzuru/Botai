"""The migration chain must produce exactly the schema the models describe."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from botai.db import models  # noqa: F401  (register tables)
from botai.db.base import Base

pytestmark = pytest.mark.db


def _diff(conn: Connection) -> list[object]:
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return list(compare_metadata(ctx, Base.metadata))


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    """`alembic revision --autogenerate` on a freshly migrated database must find nothing."""
    async with engine.connect() as conn:
        diff = await conn.run_sync(_diff)
    assert diff == [], f"models and migrations drifted, add a migration: {diff}"


async def test_one_alembic_version_row(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("select version_num from alembic_version"))).all()
    assert len(rows) == 1


async def test_secrets_unique_slot_exists(engine: AsyncEngine) -> None:
    """PgVault upserts on (user_id, connection_id, kind): the constraint must exist."""
    async with engine.connect() as conn:
        cols = (
            (
                await conn.execute(
                    text(
                        """
                    select array_agg(a.attname order by a.attname)
                    from pg_constraint c
                    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any(c.conkey)
                    where c.conrelid = 'secrets'::regclass and c.contype = 'u'
                    group by c.oid
                    """
                    )
                )
            )
            .scalars()
            .all()
        )
    assert ["connection_id", "kind", "user_id"] in cols
