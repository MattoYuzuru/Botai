"""The real pgvector path: detection, dual writes, SQL ordering, honest fallback, backfill.

These tests run against the migrated test database, where the `vector` extension and
`chunk_embeddings.embedding_vec vector(384)` exist. They prove the SQL branch is *used* (not just
compiled) and that it finds a semantically close chunk that the bounded JSONB scan cannot see.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select, text

from botai.db.models import Chunk, ChunkEmbedding
from botai.search import pgvector
from botai.search.embeddings import HashingEmbeddings, ScriptedEmbeddings
from botai.search.pgvector import PgVectorState, backfill_vectors, state_for
from botai.search.rag import RagIndexer, RagQueryService, RagSettings
from tests.search.support import RagSeed, SessionFactory

DIMENSION = 384
NEAR_TEXT = "дедлайн по машинному обучению"
QUERY = "срок сдачи работы по ML"
NOISE_CHUNKS = 40
NEAR_VECTOR = [1.0] + [0.0] * (DIMENSION - 1)


def _noise_vector(index: int) -> list[float]:
    vector = [0.0] * DIMENSION
    vector[1 + index] = 1.0
    return vector


def _scripted_provider() -> ScriptedEmbeddings:
    vectors = {
        NEAR_TEXT: NEAR_VECTOR,
        QUERY: NEAR_VECTOR,
        **{f"шумовой фрагмент {index}": _noise_vector(index) for index in range(NOISE_CHUNKS)},
    }
    return ScriptedEmbeddings(vectors, dimension=DIMENSION)


async def test_detection_sees_extension_column_and_width(sessions: SessionFactory) -> None:
    async with sessions() as session:
        state = await state_for(session)
    assert state.available is True
    assert state.dimension == DIMENSION
    assert state.reason is None
    assert pgvector.backend_for(state, DIMENSION) == "pgvector"
    assert pgvector.backend_for(state, 64) == "jsonb"


async def test_schema_has_vector_column_and_hnsw_index(sessions: SessionFactory) -> None:
    async with sessions() as session:
        column = await session.scalar(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'chunk_embeddings'::regclass AND attname = 'embedding_vec' "
                "AND NOT attisdropped"
            )
        )
        index = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'ix_chunk_embeddings_embedding_vec_hnsw'"
            )
        )
    assert column == f"vector({DIMENSION})"
    assert index is not None
    assert "hnsw" in str(index)
    assert "vector_cosine_ops" in str(index)


async def test_indexer_writes_jsonb_and_vector_together(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    text = "Дедлайн по матанализу — 15 ноября. Сдать в личном кабинете."
    settings = RagSettings(dimension=DIMENSION, chunk_chars=200, chunk_overlap=20)
    indexer = RagIndexer(sessions, HashingEmbeddings(DIMENSION), settings)
    report = await indexer.ingest_text(
        user_id=user_id, space_id=space_id, title="Заметка", text=text
    )
    assert report.embedded == report.chunks >= 1
    async with sessions() as session:
        rows = list(
            await session.scalars(select(ChunkEmbedding).where(ChunkEmbedding.user_id == user_id))
        )
    assert len(rows) == report.chunks
    for row in rows:
        assert row.embedding_vec is not None, "the derived column must be written on ingest"
        assert len(row.embedding_vec) == DIMENSION
        assert row.embedding_vec == pytest.approx(row.embedding, abs=1e-5)


async def test_reindex_fills_vector_column_for_old_jsonb_rows(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    """A corpus embedded before the pgvector revision is not re-embedded, only backfilled."""
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    document_id = await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Старый материал",
        chunks=["Дедлайн по матанализу"],
    )
    async with sessions() as session:
        chunk = await session.scalar(select(Chunk).where(Chunk.document_id == document_id))
    assert chunk is not None
    await rag.chunk_embedding(
        user_id=user_id,
        chunk_id=chunk.id,
        document_id=document_id,
        model="local-hash-v1",
        vector=[0.5] + [0.0] * (DIMENSION - 1),
    )
    settings = RagSettings(dimension=DIMENSION)
    indexer = RagIndexer(sessions, HashingEmbeddings(DIMENSION), settings)
    report = await indexer.ingest_document(user_id=user_id, document_id=document_id)
    assert report.embedded == 0
    assert report.deduplicated == 1
    assert "vectors_filled:1" in report.reasons
    async with sessions() as session:
        row = await session.scalar(
            select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == chunk.id)
        )
    assert row is not None
    assert row.embedding_vec is not None


async def test_backfill_only_touches_rows_of_the_column_width(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    wide_document = await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Широкий",
        chunks=["Вектор ширины 384"],
    )
    narrow_document = await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Узкий",
        chunks=["Вектор ширины 64"],
    )
    async with sessions() as session:
        wide_chunk = await session.scalar(select(Chunk).where(Chunk.document_id == wide_document))
        narrow_chunk = await session.scalar(
            select(Chunk).where(Chunk.document_id == narrow_document)
        )
    assert wide_chunk is not None
    assert narrow_chunk is not None
    await rag.chunk_embedding(
        user_id=user_id,
        chunk_id=wide_chunk.id,
        document_id=wide_document,
        model="local-hash-v1",
        vector=[0.25] * DIMENSION,
    )
    await rag.chunk_embedding(
        user_id=user_id,
        chunk_id=narrow_chunk.id,
        document_id=narrow_document,
        model="local-hash-v1",
        vector=[0.5] * 64,
    )
    async with sessions() as session:
        report = await backfill_vectors(session, user_id=user_id)
        await session.commit()
    assert report.available is True
    assert report.dimension == DIMENSION
    assert report.filled == 1, "only the row whose JSONB matches the column width is fillable"
    async with sessions() as session:
        wide = await session.scalar(
            select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == wide_chunk.id)
        )
        narrow = await session.scalar(
            select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == narrow_chunk.id)
        )
        again = await backfill_vectors(session, user_id=user_id)
    assert wide is not None
    assert wide.embedding_vec is not None
    assert narrow is not None
    assert narrow.embedding_vec is None
    assert again.filled == 0


async def _seed_semantic_corpus(
    rag: RagSeed,
    sessions: SessionFactory,
    provider: ScriptedEmbeddings,
) -> str:
    """One semantically near document (embedded first) plus enough noise to fill the JSONB scan."""
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    near_document = await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="ML: дедлайн",
        chunks=[NEAR_TEXT],
    )
    indexer = RagIndexer(sessions, provider, RagSettings(dimension=DIMENSION))
    await indexer.ingest_document(user_id=user_id, document_id=near_document)
    for index in range(NOISE_CHUNKS):
        document_id = await rag.document_with_chunks(
            user_id=user_id,
            space_id=space_id,
            title=f"Шум {index}",
            chunks=[f"шумовой фрагмент {index}"],
        )
        await indexer.ingest_document(user_id=user_id, document_id=document_id)
    return user_id


async def test_vector_branch_finds_what_the_bounded_jsonb_scan_misses(
    rag: RagSeed, sessions: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pgvector leg answers from a real SQL `<=>` query, not only from the JSONB scan.

    The near chunk is the oldest embedding; the JSONB scan is bounded (vector_scan_limit=2), so it
    only sees the newest noise. The SQL branch orders by distance over the whole indexed set, so it
    returns the semantically close chunk that the fallback cannot reach.
    """
    provider = _scripted_provider()
    user_id = await _seed_semantic_corpus(rag, sessions, provider)
    settings = RagSettings(dimension=DIMENSION, vector_scan_limit=2)
    service = RagQueryService(sessions, provider, settings)

    answer = await service.search(user_id=user_id, query=QUERY, limit=1)
    assert answer.vector_backend == "pgvector"
    assert answer.results, "the vector leg must return the semantically close chunk"
    assert answer.results[0].title == "ML: дедлайн"
    assert "vector" in answer.results[0].reasons

    # Force the honest JSONB fallback: same data, same provider, bounded scan.
    pgvector.reset_state_cache()
    monkeypatch.setattr(
        pgvector,
        "_state",
        PgVectorState(available=False, reason="test: column missing"),
    )
    fallback = await service.search(user_id=user_id, query=QUERY, limit=1)
    assert fallback.vector_backend == "jsonb"
    assert "ML: дедлайн" not in {hit.title for hit in fallback.results}


async def test_dimension_mismatch_degrades_to_jsonb_and_warns_once(
    rag: RagSeed, sessions: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider of another width cannot use vector(384); results stay correct via JSONB."""
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    document_id = await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Расписание",
        chunks=["Расписание пар на неделю"],
    )
    async with sessions() as session:
        chunk = await session.scalar(select(Chunk).where(Chunk.document_id == document_id))
    assert chunk is not None
    await rag.chunk_embedding(
        user_id=user_id,
        chunk_id=chunk.id,
        document_id=document_id,
        model="local-hash-v1",
        vector=[1.0] + [0.0] * 63,
    )
    provider = HashingEmbeddings(64)
    service = RagQueryService(sessions, provider, RagSettings(dimension=64))
    with caplog.at_level(logging.WARNING, logger="botai.search.pgvector"):
        first = await service.search(user_id=user_id, query="расписание пар", limit=3)
        second = await service.search(user_id=user_id, query="расписание пар", limit=3)
    assert first.vector_backend == "jsonb"
    assert first.mode == "hybrid", "the JSONB vector leg still runs; this is not a degradation"
    assert first.results
    assert first.results[0].title == "Расписание"
    assert second.results
    warnings = [record for record in caplog.records if record.name == "botai.search.pgvector"]
    assert len(warnings) == 1, "the mismatch is reported once per process, not per query"
    message = warnings[0].getMessage()
    assert "384" in message
    assert "64" in message


async def test_unavailable_column_keeps_the_jsonb_path(
    rag: RagSeed, sessions: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without pgvector the exact same search still answers from JSONB; it only says so."""
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Материалы",
        chunks=["Дедлайн по матанализу 15 ноября"],
    )
    pgvector.reset_state_cache()
    monkeypatch.setattr(
        pgvector, "_state", PgVectorState(available=False, reason="extension_missing")
    )
    service = RagQueryService(
        sessions, HashingEmbeddings(DIMENSION), RagSettings(dimension=DIMENSION)
    )
    answer = await service.search(user_id=user_id, query="дедлайн по матанализу", limit=5)
    assert answer.vector_backend == "jsonb"
    assert answer.results
    assert answer.results[0].title == "Материалы"
    assert "fts" in answer.results[0].reasons
