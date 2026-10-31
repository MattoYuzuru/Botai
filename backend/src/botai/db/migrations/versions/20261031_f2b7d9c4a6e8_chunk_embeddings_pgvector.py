"""Real pgvector storage for RAG embeddings, next to the JSONB source of truth (D-011, 18-rag §3).

Additive only, three steps:

1. `CREATE EXTENSION IF NOT EXISTS vector` — dev, CI and production run `pgvector/pgvector:pg17`
   (docs/v4/18-rag.md §3), so the extension is available; the statement is idempotent and safe to
   re-run. It stays inside the migration transaction: it is transactional in Postgres.
2. `chunk_embeddings.embedding_vec vector(<dimension>)` where the dimension is
   `BOTAI_RAG_DIMENSION` (default 384 — the dimension of the default local provider). It is fixed at
   migration time on purpose: pgvector needs a fixed width for the index, and the provider must
   produce the same width. A provider of another dimension does not fail the search — the vector
   branch degrades to the JSONB path and logs once (search/pgvector.py); switching the width is a new
   migration plus a reindex, not a silent change.
3. Existing rows are backfilled from the JSONB canonical value in one bounded `UPDATE`, then the
   HNSW cosine index is built CONCURRENTLY outside the transaction (the pattern of
   d9f3a1c7e582). Without the backfill the new vector branch would silently ignore the corpus that
   was already embedded before this revision.

`embedding` (JSONB) stays the source of truth and the portable fallback: the indexer writes both
representations, and search reads vectors from `embedding_vec` only when the extension, the column
and the dimension all match. A row whose JSONB length differs from the column width is left with
`embedding_vec IS NULL` (it can still be found on the JSONB path) — the column typmod would reject
it anyway.

The downgrade drops the index and the column and deliberately keeps the `vector` extension
installed: extension creation/drop is a cluster-level operation shared with anything else that may
use it, and dropping it cannot be undone cheaply; the next upgrade reuses it instantly.

Revision ID: f2b7d9c4a6e8
Revises: d6b8f2a14c73
Create Date: 2026-10-31
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b7d9c4a6e8"
down_revision: str | None = "d6b8f2a14c73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_chunk_embeddings_embedding_vec_hnsw"
DEFAULT_DIMENSION = 384
# pgvector's HNSW index supports fixed-width vectors up to this many dimensions.
MIN_DIMENSION = 8
MAX_DIMENSION = 2000


def _dimension() -> int:
    """The vector width the configured provider produces; the same value search checks at runtime."""
    raw = os.environ.get("BOTAI_RAG_DIMENSION", str(DEFAULT_DIMENSION)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(
            f"BOTAI_RAG_DIMENSION must be an integer, got {raw!r}; "
            "embedding_vec would get a broken fixed width"
        ) from None
    if not MIN_DIMENSION <= value <= MAX_DIMENSION:
        raise RuntimeError(
            f"BOTAI_RAG_DIMENSION must be between {MIN_DIMENSION} and {MAX_DIMENSION}, got {value}"
        )
    return value


def upgrade() -> None:
    dimension = _dimension()
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    op.execute(
        sa.text(
            "ALTER TABLE chunk_embeddings "
            f"ADD COLUMN IF NOT EXISTS embedding_vec vector({dimension})"
        )
    )

    # Backfill: JSONB array -> real[] (ordered by array position) -> vector. Only rows of the same
    # width are converted; a length mismatch stays NULL rather than aborting the migration.
    op.execute(sa.text("SET LOCAL statement_timeout = '120s'"))
    # `dimension` is a validated int, so the interpolation is not an injection surface.
    backfill = f"""
        UPDATE chunk_embeddings AS target
        SET embedding_vec = filled.vec
        FROM (
            SELECT item.id AS id,
                   array_agg(value::real ORDER BY position)::vector AS vec
            FROM chunk_embeddings AS item,
                 jsonb_array_elements_text(item.embedding) WITH ORDINALITY AS e(value, position)
            WHERE item.embedding_vec IS NULL
              AND jsonb_array_length(item.embedding) = {dimension}
            GROUP BY item.id
        ) AS filled
        WHERE target.id = filled.id
        """  # noqa: S608
    op.execute(sa.text(backfill))

    # CONCURRENTLY: the build does not lock writes to `chunk_embeddings`. A build that failed halfway
    # leaves an INVALID index of the same name, which IF NOT EXISTS would keep: drop it first.
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
        op.create_index(
            INDEX,
            "chunk_embeddings",
            ["embedding_vec"],
            postgresql_using="hnsw",
            postgresql_ops={"embedding_vec": "vector_cosine_ops"},
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
    op.drop_column("chunk_embeddings", "embedding_vec")
    # The `vector` extension is deliberately left installed: it is cluster-level, may already be
    # used by other code, and dropping it would break a later upgrade that expects it to exist.
