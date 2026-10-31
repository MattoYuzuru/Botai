"""The pgvector migration on a disposable database: extension, column, backfill, index, rollback.

The migration is additive: it must not touch the JSONB source of truth, must fill `embedding_vec`
for rows embedded before the revision, must leave the `vector` extension installed on downgrade
(only the column and the index go), and must re-upgrade cleanly.
"""

from __future__ import annotations

import asyncio
import json
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
# The revision this migration chains after: the schema without the vector column.
BEFORE = "d6b8f2a14c73"
DIMENSION = 384
VECTOR_A = [round(0.001 * index, 4) for index in range(DIMENSION)]
VECTOR_B = [0.5, 0.5, 0.0, 0.0]


def _config(target_url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "src/botai/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", target_url)
    return cfg


async def _execute(target_url: str, sql: str, **params: object) -> None:
    engine = create_async_engine(target_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


async def _fetch(target_url: str, sql: str, **params: object) -> list[tuple[object, ...]]:
    engine = create_async_engine(target_url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return [tuple(row) for row in result]
    finally:
        await engine.dispose()


async def _seed_embedded_row(
    target_url: str, *, row_id: str, vector: list[float], order: int
) -> None:
    """One chunk_embeddings row written before the pgvector revision (JSONB only)."""
    await _execute(
        target_url,
        "INSERT INTO chunks (id, user_id, document_id, ord, text, tokens) "
        "VALUES (:chunk_id, 'u1', 'd1', :ord, 'текст', 1)",
        chunk_id=f"chunk-{row_id}",
        ord=order,
    )
    await _execute(
        target_url,
        "INSERT INTO chunk_embeddings "
        "(id, user_id, chunk_id, document_id, model, dimension, content_hash, embedding) "
        "VALUES (:id, 'u1', :chunk_id, 'd1', 'local-hash-v1', :dimension, :hash, :embedding)",
        id=row_id,
        chunk_id=f"chunk-{row_id}",
        dimension=len(vector),
        hash="a" * 64,
        embedding=json.dumps(vector),
    )


async def test_pgvector_migration_round_trip(database_url: str) -> None:
    name = f"botai_pgvector_{uuid.uuid4().hex[:10]}"
    root_url, _, _ = database_url.rpartition("/")
    target_url = f"{root_url}/{name}"
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    cfg = _config(target_url)
    try:
        head = ScriptDirectory.from_config(cfg).get_current_head()
        assert head is not None
        assert head != BEFORE, "the pgvector migration must be a new revision, not rewrite history"
        assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1

        await asyncio.to_thread(command.upgrade, cfg, BEFORE)
        await _execute(
            target_url,
            "INSERT INTO users (id, email, display_name, password_hash, is_owner, disabled) "
            "VALUES ('u1', 'u1@fakeuni.test', 'Fixture', 'x', false, false)",
        )
        await _execute(
            target_url,
            "INSERT INTO spaces (id, user_id, title, accent_hue, time_zone, profile) "
            "VALUES ('s1', 'u1', 'Вуз', 250, 'Europe/Moscow', '{}')",
        )
        await _execute(
            target_url,
            "INSERT INTO documents (id, user_id, space_id, kind, title, fetched_at, hash) "
            "VALUES ('d1', 'u1', 's1', 'page', 'Материал', now(), :hash)",
            hash="b" * 64,
        )
        await _execute(
            target_url,
            "INSERT INTO chunks (id, user_id, document_id, ord, text, tokens) "
            "VALUES ('c1', 'u1', 'd1', 0, 'текст', 1)",
        )
        await _seed_embedded_row(target_url, row_id="e384", vector=VECTOR_A, order=1)
        await _seed_embedded_row(target_url, row_id="e64", vector=VECTOR_B, order=2)
        columns = await _fetch(
            target_url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'chunk_embeddings' AND column_name = 'embedding_vec'",
        )
        assert columns == [], "the vector column must only appear with this migration"

        await asyncio.to_thread(command.upgrade, cfg, "head")

        extension = await _fetch(
            target_url, "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        assert extension
        assert extension[0][0]
        column = await _fetch(
            target_url,
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'chunk_embeddings'::regclass AND attname = 'embedding_vec' "
            "AND NOT attisdropped",
        )
        assert column == [(f"vector({DIMENSION})",)]
        index = await _fetch(
            target_url,
            "SELECT indexdef FROM pg_indexes WHERE indexname = "
            "'ix_chunk_embeddings_embedding_vec_hnsw'",
        )
        assert len(index) == 1
        indexdef = str(index[0][0])
        assert "USING hnsw" in indexdef
        assert "vector_cosine_ops" in indexdef

        # The migration backfills existing JSONB rows of the column width and nothing else.
        filled = await _fetch(
            target_url, "SELECT id, embedding_vec::text FROM chunk_embeddings ORDER BY id"
        )
        values = {row[0]: row[1] for row in filled}
        assert values["e384"] is not None
        parsed = [float(part) for part in str(values["e384"]).strip("[]").split(",")]
        assert len(parsed) == DIMENSION
        for expected, actual in zip(VECTOR_A, parsed, strict=True):
            assert actual == pytest.approx(expected, abs=1e-5)
        assert values["e64"] is None, "a row of another width cannot be stored in vector(384)"
        canonical = await _fetch(
            target_url, "SELECT embedding FROM chunk_embeddings WHERE id = 'e384'"
        )
        assert canonical[0][0] is not None, "JSONB stays the source of truth"

        # The HNSW index is not just present but usable for the distance order.
        engine = create_async_engine(target_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SET LOCAL enable_seqscan = off"))
                plan = await conn.execute(
                    text(
                        "EXPLAIN SELECT id FROM chunk_embeddings "
                        "WHERE embedding_vec IS NOT NULL "
                        "ORDER BY embedding_vec <=> CAST(CAST(:q AS real[]) AS vector(384)) "
                        "LIMIT 5"
                    ),
                    {"q": VECTOR_A},
                )
                plan_text = "\n".join(str(row[0]) for row in plan)
        finally:
            await engine.dispose()
        assert "ix_chunk_embeddings_embedding_vec_hnsw" in plan_text

        await asyncio.to_thread(command.downgrade, cfg, "-1")
        columns = await _fetch(
            target_url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'chunk_embeddings' AND column_name = 'embedding_vec'",
        )
        assert columns == []
        indexes = await _fetch(
            target_url,
            "SELECT indexname FROM pg_indexes WHERE indexname = "
            "'ix_chunk_embeddings_embedding_vec_hnsw'",
        )
        assert indexes == []
        extension = await _fetch(
            target_url, "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        assert extension, "downgrade deliberately leaves the extension installed"
        canonical = await _fetch(
            target_url, "SELECT embedding FROM chunk_embeddings WHERE id = 'e384'"
        )
        assert canonical[0][0] is not None, "downgrade keeps the JSONB corpus"

        await asyncio.to_thread(command.upgrade, cfg, "head")
        again = await _fetch(
            target_url, "SELECT embedding_vec IS NOT NULL FROM chunk_embeddings WHERE id = 'e384'"
        )
        assert again == [(True,)], "a re-upgrade backfills the column again"
        version = await _fetch(target_url, "SELECT version_num FROM alembic_version")
        assert version[0][0] == head
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
