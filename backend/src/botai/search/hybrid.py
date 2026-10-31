"""Hybrid retrieval over the student's material: Russian FTS + vector, fusion, rerank, provenance.

The module does not replace the v3 FTS search (`botai.agent.tools.search_documents`): it is a new
path that adds embeddings on top of the same `chunks`/`documents` rows and returns citations with
freshness and provenance. A chunk found only by FTS is still a result — the vector leg is an
improvement, not a requirement, so a missing embedding never hides text.

Ranking, in order of influence:

1. reciprocal-rank fusion of the FTS and vector legs (each leg's order matters, not its raw score);
2. lexical overlap with the query (keeps an exact name above a merely close meaning);
3. freshness of the material (exponential decay by capture/fetch time);
4. a small boost for a `high` trust source.

`VectorStore`/`FtsIndex` are plain functions over one `AsyncSession`. The vector leg has two
physical implementations: the JSONB bounded scan (portable default) and one SQL `ORDER BY
embedding_vec <=> query LIMIT k` when the pgvector column exists and its width matches the provider
(`botai.search.pgvector`, docs/v4/18-rag.md §3). Both return the same `ChunkCandidate` rows; the
result says which one ran (`vector_backend`) and a width mismatch degrades to JSONB, never to a
silent empty leg.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import Chunk, ChunkEmbedding, Document
from botai.domain.repo import LIVE_DOCUMENT
from botai.search.embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingUsage,
    cosine_similarity,
    normalize,
)
from botai.search.pgvector import (
    PgVectorState,
    backend_for,
    cosine_from_distance,
    distance,
    note_dimension_mismatch,
    state_for,
)

RRF_K = 60.0
DEFAULT_RERANK = True
DEFAULT_STALE_AFTER_DAYS = 30.0
TEXT_PREVIEW_CHARS = 600
_WORD = re.compile(r"\w+")


# --- data ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HybridFilters:
    """Narrowing that does not change the ranking: artifact/document kinds, sources, freshness."""

    kinds: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    fresh_after: datetime | None = None

    def empty(self) -> bool:
        return not self.kinds and not self.source_ids and self.fresh_after is None


@dataclass(frozen=True, slots=True)
class ChunkCandidate:
    """One retrieved chunk with the provenance the result needs."""

    chunk_id: str
    document_id: str
    text: str
    title: str
    url: str | None
    kind: str
    fetched_at: datetime
    source_id: str | None = None
    artifact_id: str | None = None
    captured_at: datetime | None = None
    trust: str | None = None
    fts_rank: float = 0.0
    vector_score: float = 0.0


@dataclass(frozen=True, slots=True)
class HybridHit:
    """A ranked result with its citation, freshness and provenance."""

    chunk_id: str
    document_id: str
    title: str
    url: str | None
    text: str
    kind: str
    fetched_at: datetime
    source_id: str | None
    artifact_id: str | None
    captured_at: datetime | None
    trust: str | None
    score: float
    fts_rank: float
    vector_score: float
    age_days: float | None
    stale: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HybridSearchResult:
    """The whole retrieval answer: hits plus honest degradation and spend."""

    hits: tuple[HybridHit, ...]
    mode: str  # "hybrid" | "fts_only"
    degraded: str | None = None
    # Which physical vector leg ran: "pgvector" (SQL <=> LIMIT k), "jsonb" (bounded scan), "none".
    # Reported so a width mismatch or a missing column is visible instead of silently slower.
    vector_backend: str = "jsonb"
    usage: EmbeddingUsage = field(default_factory=EmbeddingUsage)


# --- query helpers -------------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {word.casefold() for word in _WORD.findall(text) if len(word) > 2}


def lexical_overlap(query: str, text: str) -> float:
    """Share of the query words present in the chunk (0..1)."""
    wanted = _tokens(query)
    if not wanted:
        return 0.0
    found = wanted & _tokens(text)
    return len(found) / len(wanted)


def _or_query(text: str) -> str:
    words = [word for word in _WORD.findall(text.casefold()) if len(word) > 2][:12]
    return " | ".join(dict.fromkeys(words))


def _tsqueries(query: str) -> list[Any]:
    """Strict first (all words), then lenient (any word), as the v3 FTS search does."""
    statements: list[Any] = [func.websearch_to_tsquery("russian", query)]
    if lenient := _or_query(query):
        statements.append(func.to_tsquery("russian", lenient))
    return statements


def _freshness(reference: datetime | None, now: datetime, half_life_days: float) -> float:
    """1.0 for material captured now, 0.5 after the half-life; unknown time is neutral."""
    if reference is None:
        return 1.0
    age_days = max((now - reference).total_seconds(), 0.0) / 86_400.0
    return 0.5 ** (age_days / max(half_life_days, 0.1))


def _age_days(reference: datetime | None, now: datetime) -> float | None:
    if reference is None:
        return None
    return round(max((now - reference).total_seconds(), 0.0) / 86_400.0, 3)


# --- retrieval legs ------------------------------------------------------------------------------


def _reference_time(candidate: ChunkCandidate) -> datetime | None:
    return candidate.captured_at or candidate.fetched_at


def _candidate(
    chunk: Chunk,
    document: Document,
    provenance: ChunkEmbedding | None,
    *,
    fts_rank: float = 0.0,
    vector_score: float = 0.0,
) -> ChunkCandidate:
    kind = ""
    if provenance is not None and provenance.kind:
        kind = provenance.kind
    return ChunkCandidate(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        text=chunk.text,
        title=document.title,
        url=document.url,
        kind=kind or document.kind,
        fetched_at=document.fetched_at,
        source_id=provenance.source_id if provenance is not None else None,
        artifact_id=provenance.artifact_id if provenance is not None else None,
        captured_at=provenance.captured_at if provenance is not None else None,
        trust=provenance.trust if provenance is not None else None,
        fts_rank=fts_rank,
        vector_score=vector_score,
    )


def _filters_sql(filters: HybridFilters) -> list[Any]:
    """SQL narrowing shared by both legs; provenance columns live in `chunk_embeddings`."""
    conditions: list[Any] = []
    if filters.kinds:
        conditions.append(
            func.coalesce(ChunkEmbedding.kind, Document.kind).in_(list(filters.kinds))
        )
    if filters.source_ids:
        conditions.append(ChunkEmbedding.source_id.in_(list(filters.source_ids)))
    if filters.fresh_after is not None:
        conditions.append(
            func.coalesce(ChunkEmbedding.captured_at, Document.fetched_at) >= filters.fresh_after
        )
    return conditions


async def fts_candidates(
    session: AsyncSession,
    *,
    user_id: str,
    query: str,
    model: str,
    limit: int,
    filters: HybridFilters,
) -> list[ChunkCandidate]:
    """The FTS leg: Russian full-text rank over the user's live documents."""
    base = (
        select(Chunk, Document, ChunkEmbedding)
        .join(Document, Document.id == Chunk.document_id)
        .outerjoin(
            ChunkEmbedding,
            and_(ChunkEmbedding.chunk_id == Chunk.id, ChunkEmbedding.model == model),
        )
        .where(Chunk.user_id == user_id, Document.user_id == user_id, LIVE_DOCUMENT)
    )
    for condition in _filters_sql(filters):
        base = base.where(condition)
    for tsquery in _tsqueries(query):
        rank = func.ts_rank_cd(Chunk.tsv, tsquery)
        statement = (
            base.add_columns(rank.label("rank"))
            .where(Chunk.tsv.op("@@")(tsquery))
            .order_by(rank.desc(), Document.fetched_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(statement)).all()
        if rows:
            return [
                _candidate(
                    chunk,
                    document,
                    provenance,
                    fts_rank=float(rank_value or 0.0),
                )
                for chunk, document, provenance, rank_value in rows
            ]
    return []


async def vector_candidates(
    session: AsyncSession,
    *,
    user_id: str,
    query_vector: list[float],
    model: str,
    limit: int,
    scan_limit: int,
    filters: HybridFilters,
    state: PgVectorState | None = None,
) -> list[ChunkCandidate]:
    """The vector leg: `pgvector` SQL when the column and the width match, else the JSONB scan.

    `state` lets a caller that already detected pgvector availability reuse the answer; without it
    the process-wide detection runs once. A width mismatch is logged once (dimensions only) and
    falls back to the JSONB path, which computes the same cosine in Python.
    """
    resolved = state or await state_for(session)
    if resolved.available and resolved.dimension is not None:
        if resolved.dimension == len(query_vector):
            return await _pgvector_candidates(
                session,
                user_id=user_id,
                query_vector=query_vector,
                model=model,
                limit=limit,
                filters=filters,
                dimension=resolved.dimension,
            )
        note_dimension_mismatch(
            provider_dimension=len(query_vector), column_dimension=resolved.dimension
        )
    return await _jsonb_candidates(
        session,
        user_id=user_id,
        query_vector=query_vector,
        model=model,
        limit=limit,
        scan_limit=scan_limit,
        filters=filters,
    )


async def _pgvector_candidates(
    session: AsyncSession,
    *,
    user_id: str,
    query_vector: list[float],
    model: str,
    limit: int,
    filters: HybridFilters,
    dimension: int,
) -> list[ChunkCandidate]:
    """One SQL statement: cosine distance ordered by the HNSW-indexed `embedding_vec` column."""
    score = distance(ChunkEmbedding.embedding_vec, query_vector, dimension=dimension)
    statement = (
        select(Chunk, Document, ChunkEmbedding, score.label("distance"))
        .join(Document, Document.id == Chunk.document_id)
        .join(
            ChunkEmbedding,
            and_(ChunkEmbedding.chunk_id == Chunk.id, ChunkEmbedding.model == model),
        )
        .where(
            Chunk.user_id == user_id,
            Document.user_id == user_id,
            LIVE_DOCUMENT,
            ChunkEmbedding.user_id == user_id,
            ChunkEmbedding.embedding_vec.is_not(None),
        )
        .order_by(score)
        .limit(limit)
    )
    for condition in _filters_sql(filters):
        statement = statement.where(condition)
    rows = (await session.execute(statement)).all()
    return [
        _candidate(
            chunk,
            document,
            provenance,
            vector_score=cosine_from_distance(distance_value),
        )
        for chunk, document, provenance, distance_value in rows
    ]


async def _jsonb_candidates(
    session: AsyncSession,
    *,
    user_id: str,
    query_vector: list[float],
    model: str,
    limit: int,
    scan_limit: int,
    filters: HybridFilters,
) -> list[ChunkCandidate]:
    """The portable fallback: a bounded scan of the JSONB vectors plus cosine in Python.

    `scan_limit` bounds the work on a pet-project corpus (thousands of chunks). This path is also
    what runs while the pgvector column does not exist or has another width.
    """
    statement = (
        select(Chunk, Document, ChunkEmbedding)
        .join(Document, Document.id == Chunk.document_id)
        .join(
            ChunkEmbedding,
            and_(ChunkEmbedding.chunk_id == Chunk.id, ChunkEmbedding.model == model),
        )
        .where(
            Chunk.user_id == user_id,
            Document.user_id == user_id,
            LIVE_DOCUMENT,
            ChunkEmbedding.user_id == user_id,
        )
        .order_by(ChunkEmbedding.created_at.desc())
        .limit(max(limit, scan_limit))
    )
    for condition in _filters_sql(filters):
        statement = statement.where(condition)
    rows = (await session.execute(statement)).all()
    scored: list[ChunkCandidate] = []
    for chunk, document, provenance in rows:
        vector = [float(value) for value in provenance.embedding]
        scored.append(
            _candidate(
                chunk,
                document,
                provenance,
                vector_score=cosine_similarity(query_vector, vector),
            )
        )
    scored.sort(key=lambda item: item.vector_score, reverse=True)
    return scored[:limit]


# --- fusion --------------------------------------------------------------------------------------


def merge_ranks(
    fts: list[ChunkCandidate],
    vector: list[ChunkCandidate],
    *,
    query: str,
    limit: int,
    now: datetime,
    rerank: bool = DEFAULT_RERANK,
    half_life_days: float = 30.0,
    stale_after_days: float = DEFAULT_STALE_AFTER_DAYS,
) -> list[HybridHit]:
    """Reciprocal-rank fusion + lexical/freshness rerank over both legs."""
    fused: dict[str, dict[str, Any]] = {}
    for rank, candidate in enumerate(fts, start=1):
        entry = fused.setdefault(candidate.chunk_id, {"candidate": candidate, "rrf": 0.0})
        entry["rrf"] = float(entry["rrf"]) + 1.0 / (RRF_K + rank)
        entry["candidate"] = _prefer_provenance(candidate, entry["candidate"])
    for rank, candidate in enumerate(vector, start=1):
        entry = fused.setdefault(candidate.chunk_id, {"candidate": candidate, "rrf": 0.0})
        entry["rrf"] = float(entry["rrf"]) + 1.0 / (RRF_K + rank)
        entry["candidate"] = _prefer_provenance(candidate, entry["candidate"])
    if not fused:
        return []
    best_rrf = max(float(entry["rrf"]) for entry in fused.values())
    hits: list[HybridHit] = []
    for entry in fused.values():
        candidate: ChunkCandidate = entry["candidate"]
        rrf_score = float(entry["rrf"]) / best_rrf if best_rrf else 0.0
        overlap = lexical_overlap(query, candidate.text)
        reference = _reference_time(candidate)
        freshness = _freshness(reference, now, half_life_days)
        score = 0.65 * rrf_score + 0.20 * overlap + 0.15 * freshness if rerank else rrf_score
        reasons: list[str] = []
        if candidate.fts_rank > 0:
            reasons.append("fts")
        if candidate.vector_score > 0:
            reasons.append("vector")
        if overlap > 0:
            reasons.append("lexical")
        if freshness >= 0.5:
            reasons.append("fresh")
        if candidate.trust == "high":
            reasons.append("high_trust")
            score += 0.03 if rerank else 0.0
        age = _age_days(reference, now)
        hits.append(
            HybridHit(
                chunk_id=candidate.chunk_id,
                document_id=candidate.document_id,
                title=candidate.title,
                url=candidate.url,
                text=candidate.text,
                kind=candidate.kind,
                fetched_at=candidate.fetched_at,
                source_id=candidate.source_id,
                artifact_id=candidate.artifact_id,
                captured_at=candidate.captured_at,
                trust=candidate.trust,
                score=round(score, 6),
                fts_rank=round(candidate.fts_rank, 6),
                vector_score=round(candidate.vector_score, 6),
                age_days=age,
                stale=age is not None and age > stale_after_days,
                reasons=tuple(reasons),
            )
        )
    hits.sort(key=lambda hit: (-hit.score, -hit.vector_score, hit.title.casefold()))
    return hits[:limit]


def _prefer_provenance(preferred: ChunkCandidate, current: ChunkCandidate) -> ChunkCandidate:
    """Keep the row that knows where the text came from and how fresh it is."""
    current_has = current.source_id is not None or current.captured_at is not None
    preferred_has = preferred.source_id is not None or preferred.captured_at is not None
    if preferred_has and not current_has:
        return preferred
    return current


# --- public entry point --------------------------------------------------------------------------


async def hybrid_search(
    session: AsyncSession,
    *,
    user_id: str,
    query: str,
    provider: EmbeddingProvider,
    limit: int = 8,
    filters: HybridFilters | None = None,
    rerank: bool = DEFAULT_RERANK,
    vector_scan_limit: int = 500,
    half_life_days: float = 30.0,
    stale_after_days: float = DEFAULT_STALE_AFTER_DAYS,
    now: datetime | None = None,
) -> HybridSearchResult:
    """FTS + vector retrieval with honest degradation when the vector leg is unavailable."""
    moment = now or datetime.now(UTC)
    narrowed = filters or HybridFilters()
    limit = max(1, min(limit, 50))
    model = provider.name
    fts = await fts_candidates(
        session,
        user_id=user_id,
        query=query,
        model=model,
        limit=max(limit * 6, 40),
        filters=narrowed,
    )
    degraded: str | None = None
    usage = EmbeddingUsage()
    vector: list[ChunkCandidate] = []
    vector_backend = "none"
    before = provider.usage()
    try:
        vectors = await provider.embed([query])
    except EmbeddingError as exc:
        degraded = f"vector_unavailable: {exc}"
    else:
        if vectors:
            query_vector = normalize(vectors[0])
            state = await state_for(session)
            vector = await vector_candidates(
                session,
                user_id=user_id,
                query_vector=query_vector,
                model=model,
                limit=max(limit * 3, 30),
                scan_limit=vector_scan_limit,
                filters=narrowed,
                state=state,
            )
            vector_backend = backend_for(state, len(query_vector))
        usage = provider.usage().minus(before)
    hits = merge_ranks(
        fts,
        vector,
        query=query,
        limit=limit,
        now=moment,
        rerank=rerank,
        half_life_days=half_life_days,
        stale_after_days=stale_after_days,
    )
    return HybridSearchResult(
        hits=tuple(hits),
        mode="hybrid" if not degraded else "fts_only",
        degraded=degraded,
        vector_backend=vector_backend,
        usage=usage,
    )
