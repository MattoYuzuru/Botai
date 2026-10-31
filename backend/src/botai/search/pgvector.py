"""Real pgvector storage for RAG embeddings: detection, SQL distance, backfill.

`chunk_embeddings.embedding` (JSONB) is the source of truth; `embedding_vec vector(n)` is a derived
column that exists only after the pgvector migration (docs/v4/18-rag.md §3, D-011). This module owns
the one place that knows about that physical column, so the rest of the RAG code keeps one interface:

* `state_for` detects once per process whether the extension and the column exist and how wide the
  column is. Detection never raises: a database without pgvector simply gets `available=False` and
  the JSONB path keeps working.
* `distance` builds the `embedding_vec <=> CAST(CAST(:q AS real[]) AS vector(n))` expression. The
  real[] hop is deliberate: `asyncpg` encodes a Python float list for `real[]` natively, while a raw
  `::vector` parameter would need a driver codec for the extension type.
* `backfill_vectors` fills `embedding_vec` for rows embedded before the column existed, in bounded
  batches, from the canonical JSONB value. Rows of another width stay NULL (the column typmod would
  reject them) and remain reachable on the JSONB path.
* `note_dimension_mismatch` logs once, without values: a provider whose width differs from the
  column cannot use the vector branch, and degrading to JSONB must be visible in the log.

Invariants: the column width is fixed by the migration from `BOTAI_RAG_DIMENSION` (default 384) and
must equal `EmbeddingProvider.dimension`; changing the provider model or width is a new migration
plus a reindex, not a runtime switch.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Float, bindparam, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY, REAL
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import ChunkEmbedding, VectorType

VECTOR_TABLE = "chunk_embeddings"
VECTOR_COLUMN = "embedding_vec"
VECTOR_INDEX = "ix_chunk_embeddings_embedding_vec_hnsw"
DEFAULT_BATCH_SIZE = 200
DEFAULT_MAX_ROWS = 5_000
_VECTOR_WIDTH = re.compile(r"^vector\((\d+)\)$")

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PgVectorState:
    """Whether the physical vector column can be used in this process, and why not."""

    available: bool
    dimension: int | None = None
    reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.available and self.dimension is not None


@dataclass(frozen=True, slots=True)
class VectorBackfillReport:
    """What one bounded backfill pass filled."""

    filled: int = 0
    available: bool = False
    dimension: int | None = None
    reason: str | None = None


_state: PgVectorState | None = None
_mismatch_logged = False


def reset_state_cache() -> None:
    """Forget the per-process detection (tests, and a process that lives across a migration)."""
    global _state, _mismatch_logged
    _state = None
    _mismatch_logged = False


async def state_for(session: AsyncSession) -> PgVectorState:
    """The cached detection result for this process; the first caller pays for one catalog query."""
    global _state
    if _state is None:
        _state = await _detect(session)
    return _state


async def _detect(session: AsyncSession) -> PgVectorState:
    extension = await session.scalar(
        sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    )
    if not extension:
        return PgVectorState(available=False, reason="extension_missing")
    formatted = await session.scalar(
        sa.text(
            """
            SELECT format_type(attribute.atttypid, attribute.atttypmod)
            FROM pg_attribute AS attribute
            WHERE attribute.attrelid = to_regclass(:table)
              AND attribute.attname = :column
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            """
        ).bindparams(table=VECTOR_TABLE, column=VECTOR_COLUMN)
    )
    if not formatted:
        return PgVectorState(available=False, reason="column_missing")
    match = _VECTOR_WIDTH.match(str(formatted))
    if match is None:
        return PgVectorState(available=False, reason="unknown_width")
    return PgVectorState(available=True, dimension=int(match.group(1)))


def backend_for(state: PgVectorState, provider_dimension: int) -> str:
    """Which retrieval branch a query of this width will use: `pgvector` or the JSONB fallback."""
    if state.usable and state.dimension == provider_dimension:
        return "pgvector"
    return "jsonb"


def note_dimension_mismatch(*, provider_dimension: int, column_dimension: int) -> None:
    """Warn once per process that the vector branch is skipped; dimensions only, never values."""
    global _mismatch_logged
    if _mismatch_logged:
        return
    _mismatch_logged = True
    log.warning(
        "pgvector column is vector(%s) but the embedding provider produces %s dimensions: "
        "the JSONB path answers until the corpus is reindexed for one width",
        column_dimension,
        provider_dimension,
    )


def distance(column: Any, values: Sequence[float], *, dimension: int) -> Any:
    """The cosine-distance expression `column <=> CAST(CAST(:q AS real[]) AS vector(n))`."""
    query = bindparam("pgvector_query", value=[float(value) for value in values], type_=ARRAY(REAL))
    return column.op("<=>", return_type=Float)(
        cast(cast(query, ARRAY(REAL)), VectorType(dimension))
    )


def cosine_from_distance(distance_value: float | None) -> float:
    """Cosine similarity in [-1, 1] from pgvector's cosine distance; NULL means "no vector"."""
    if distance_value is None:
        return 0.0
    return max(-1.0, min(1.0, 1.0 - float(distance_value)))


async def backfill_vectors(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    model: str | None = None,
    chunk_ids: Sequence[str] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> VectorBackfillReport:
    """Fill `embedding_vec` from JSONB in bounded batches; the caller commits the session."""
    state = await state_for(session)
    if not state.usable or state.dimension is None:
        return VectorBackfillReport(available=False, reason=state.reason or "unavailable")
    dimension = state.dimension
    remaining = max(0, max_rows)
    filled = 0
    while remaining > 0:
        statement = (
            select(ChunkEmbedding)
            .where(
                ChunkEmbedding.embedding_vec.is_(None),
                func.jsonb_typeof(ChunkEmbedding.embedding) == sa.literal("array"),
                func.jsonb_array_length(ChunkEmbedding.embedding) == dimension,
            )
            .order_by(ChunkEmbedding.created_at)
            .limit(min(batch_size, remaining))
        )
        if user_id is not None:
            statement = statement.where(ChunkEmbedding.user_id == user_id)
        if model is not None:
            statement = statement.where(ChunkEmbedding.model == model)
        if chunk_ids is not None:
            statement = statement.where(ChunkEmbedding.chunk_id.in_(list(chunk_ids)))
        rows = list(await session.scalars(statement))
        if not rows:
            break
        for row in rows:
            row.embedding_vec = [float(value) for value in row.embedding]
        await session.flush()
        filled += len(rows)
        remaining -= len(rows)
    return VectorBackfillReport(filled=filled, available=True, dimension=dimension)
