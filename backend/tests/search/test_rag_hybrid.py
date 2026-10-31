"""Hybrid retrieval: semantic ranking on mock vectors, FTS, provenance, freshness, degradation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from botai.db.models import Chunk
from botai.search.embeddings import (
    EmbeddingError,
    EmbeddingUsage,
    HashingEmbeddings,
    ScriptedEmbeddings,
)
from botai.search.hybrid import (
    ChunkCandidate,
    HybridFilters,
    hybrid_search,
    lexical_overlap,
    merge_ranks,
)
from botai.search.rag import RagIndexer, RagQueryService, RagSettings
from tests.search.support import RagSeed, SessionFactory

DIMENSION = 64
CONCEPTS = {
    "дедлайн по машинному обучению": [1.0, 0.0, 0.0, 0.0],
    "срок сдачи работы по ML": [1.0, 0.0, 0.0, 0.0],
    "расписание пар на неделю": [0.0, 1.0, 0.0, 0.0],
}


def test_lexical_overlap_ignores_short_words() -> None:
    assert lexical_overlap("дедлайн по матанализу", "дедлайн по матанализу") == 1.0
    assert lexical_overlap("дедлайн по матанализу", "расписание пар") == 0.0
    assert lexical_overlap("в по на", "дедлайн") == 0.0


def test_merge_ranks_keeps_both_legs_and_prefers_the_shared_hit() -> None:
    now = datetime.now(UTC)
    shared = ChunkCandidate(
        chunk_id="shared",
        document_id="doc-1",
        text="дедлайн по матанализу",
        title="Матанализ",
        url=None,
        kind="page",
        fetched_at=now,
    )
    fts_only = ChunkCandidate(
        chunk_id="fts",
        document_id="doc-2",
        text="дедлайн",
        title="Дедлайн",
        url=None,
        kind="page",
        fetched_at=now,
    )
    vector_only = ChunkCandidate(
        chunk_id="vector",
        document_id="doc-3",
        text="срок сдачи",
        title="Срок",
        url=None,
        kind="page",
        fetched_at=now,
    )
    hits = merge_ranks([shared, fts_only], [shared, vector_only], query="дедлайн", limit=5, now=now)
    assert {hit.chunk_id for hit in hits} == {"shared", "fts", "vector"}
    assert hits[0].chunk_id == "shared"


async def test_vector_leg_ranks_the_close_meaning_above_the_unrelated_chunk(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    close_document = await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="ML: дедлайн",
        chunks=["дедлайн по машинному обучению"],
    )
    far_document = await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Расписание",
        chunks=["расписание пар на неделю"],
    )
    async with sessions() as session:
        chunks = {
            chunk.text: chunk
            for chunk in await session.scalars(select(Chunk).where(Chunk.user_id == user_id))
        }
    for text, document_id in (
        ("дедлайн по машинному обучению", close_document),
        ("расписание пар на неделю", far_document),
    ):
        await rag.chunk_embedding(
            user_id=user_id,
            chunk_id=chunks[text].id,
            document_id=document_id,
            model="mock-scripted",
            vector=CONCEPTS[text],
        )
    provider = ScriptedEmbeddings(CONCEPTS, dimension=4)
    service = RagQueryService(sessions, provider, RagSettings(dimension=4))
    answer = await service.search(user_id=user_id, query="срок сдачи работы по ML", limit=5)
    assert answer.mode == "hybrid"
    assert answer.results, "the vector leg must return the semantically close chunk"
    assert answer.results[0].title == "ML: дедлайн"
    assert "vector" in answer.results[0].reasons
    assert answer.results[0].score > answer.results[-1].score


async def test_fts_leg_works_without_any_embedding(rag: RagSeed, sessions: SessionFactory) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Расписание на неделю",
        chunks=["Расписание пар на неделю: матанализ, физика."],
    )
    service = RagQueryService(
        sessions, HashingEmbeddings(DIMENSION), RagSettings(dimension=DIMENSION)
    )
    answer = await service.search(user_id=user_id, query="расписание пар", limit=5)
    assert answer.results
    assert answer.results[0].title == "Расписание на неделю"
    assert "fts" in answer.results[0].reasons


async def test_result_carries_provenance_freshness_and_filters(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id, trust="high")
    captured = datetime.now(UTC) - timedelta(days=90)
    artifact_id = await rag.artifact(
        user_id=user_id,
        source_id=source_id,
        text="Дедлайн по матанализу — 15 ноября, сдать в личном кабинете.",
        captured_at=captured,
    )
    settings = RagSettings(dimension=DIMENSION)
    indexer = RagIndexer(sessions, HashingEmbeddings(DIMENSION), settings)
    await indexer.ingest_source(user_id=user_id, source_id=source_id)

    service = RagQueryService(sessions, HashingEmbeddings(DIMENSION), settings)
    answer = await service.search(user_id=user_id, query="дедлайн по матанализу", limit=5)
    assert answer.results
    hit = answer.results[0]
    assert hit.source_id == source_id
    assert hit.artifact_id == artifact_id
    assert hit.kind == "page"
    assert hit.captured_at is not None
    assert hit.age_days is not None
    assert hit.age_days >= 89.0
    assert hit.stale is True
    assert "high_trust" in hit.reasons

    fresh = await service.search(
        user_id=user_id,
        query="дедлайн по матанализу",
        fresh_after=datetime.now(UTC) - timedelta(days=10),
    )
    assert fresh.results == ()
    wrong_kind = await service.search(user_id=user_id, query="дедлайн", kinds=["screenshot"])
    assert wrong_kind.results == ()
    right_kind = await service.search(user_id=user_id, query="дедлайн", kinds=["page"])
    assert right_kind.results
    wrong_source = await service.search(
        user_id=user_id, query="дедлайн", source_ids=["another-source"]
    )
    assert wrong_source.results == ()
    right_source = await service.search(user_id=user_id, query="дедлайн", source_ids=[source_id])
    assert right_source.results
    assert right_source.mode == "hybrid"


async def test_search_never_returns_another_users_material(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    first = await rag.user("Первый")
    first_space = await rag.space(first)
    await rag.document_with_chunks(
        user_id=first,
        space_id=first_space,
        title="Первый: дедлайн",
        chunks=["дедлайн по матанализу 15 ноября"],
    )
    second = await rag.user("Второй")
    second_space = await rag.space(second)
    await rag.document_with_chunks(
        user_id=second,
        space_id=second_space,
        title="Второй: расписание",
        chunks=["расписание пар на неделю"],
    )
    service = RagQueryService(
        sessions, HashingEmbeddings(DIMENSION), RagSettings(dimension=DIMENSION)
    )
    first_answer = await service.search(user_id=first, query="дедлайн", limit=5)
    assert first_answer.results
    assert "Второй" not in {hit.title for hit in first_answer.results}
    second_answer = await service.search(user_id=second, query="дедлайн", limit=5)
    assert "Второй: расписание" not in {hit.title for hit in second_answer.results}


class FailingEmbeddings:
    """A provider that is down: hybrid search must degrade to FTS, not fail."""

    name = "broken-model"
    dimension = 4

    async def embed(self, texts: object) -> list[list[float]]:
        raise EmbeddingError("provider is down")

    def estimate_cost_usd(self, texts: object) -> float:
        return 0.0

    def usage(self) -> EmbeddingUsage:
        return EmbeddingUsage()


async def test_degraded_vector_leg_returns_fts_results(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Расписание",
        chunks=["Расписание пар на неделю"],
    )
    service = RagQueryService(sessions, FailingEmbeddings(), RagSettings(dimension=4))
    answer = await service.search(user_id=user_id, query="расписание", limit=5)
    assert answer.mode == "fts_only"
    assert answer.degraded
    assert "provider is down" in answer.degraded
    assert answer.results
    assert answer.results[0].title == "Расписание"


async def test_hybrid_search_accepts_explicit_filters(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Материалы",
        chunks=["Дедлайн по матанализу"],
        fetched_at=datetime.now(UTC) - timedelta(days=1),
    )
    async with sessions() as session:
        result = await hybrid_search(
            session,
            user_id=user_id,
            query="дедлайн",
            provider=HashingEmbeddings(DIMENSION),
            filters=HybridFilters(kinds=("page",)),
        )
    assert result.hits
    assert result.usage.texts >= 0
