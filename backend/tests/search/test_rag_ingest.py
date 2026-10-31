"""RAG ingestion: artifacts, provenance, dedup, skips, storage_ref, document embedding, flag."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text

from botai.agent.v4.tools.rag import build_rag_tools
from botai.db.models import Chunk, ChunkEmbedding, Document
from botai.search.embeddings import HashingEmbeddings
from botai.search.rag import DefaultArtifactTextResolver, RagIndexer, RagSettings
from tests.search.support import RagSeed, SessionFactory

DIMENSION = 64
TEXT = (
    "Дедлайн по матанализу — 15 ноября. Сдать домашнее задание нужно в личном кабинете.\n\n"
    "Экзамен пройдёт 20 декабря, аудитория будет объявлена позже."
)


def _indexer(sessions: SessionFactory, **overrides: object) -> RagIndexer:
    settings = RagSettings(
        dimension=DIMENSION,
        chunk_chars=160,
        chunk_overlap=20,
        **overrides,  # type: ignore[arg-type]
    )
    return RagIndexer(sessions, HashingEmbeddings(DIMENSION), settings)


async def test_schema_has_chunk_embeddings(sessions: SessionFactory) -> None:
    async with sessions() as session:
        names = list(
            await session.scalars(
                text(
                    "select column_name from information_schema.columns "
                    "where table_name = 'chunk_embeddings'"
                )
            )
        )
    assert {
        "id",
        "user_id",
        "chunk_id",
        "document_id",
        "source_id",
        "artifact_id",
        "model",
        "dimension",
        "content_hash",
        "embedding",
        "kind",
        "captured_at",
        "trust",
        "created_at",
    } <= set(names)


async def test_ingest_source_indexes_artifacts_with_provenance(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id)
    captured = datetime.now(UTC) - timedelta(days=2)
    artifact_id = await rag.artifact(
        user_id=user_id, source_id=source_id, text=TEXT, captured_at=captured
    )

    indexer = _indexer(sessions)
    report = await indexer.ingest_source(user_id=user_id, source_id=source_id)
    assert report.documents == 1
    assert report.chunks >= 1
    assert report.embedded == report.chunks
    assert report.usage.texts == report.chunks
    assert report.usage.cost_usd == 0.0

    async with sessions() as session:
        embeddings = list(
            await session.scalars(select(ChunkEmbedding).where(ChunkEmbedding.user_id == user_id))
        )
        documents = list(await session.scalars(select(Document).where(Document.user_id == user_id)))
    assert len(embeddings) == report.chunks
    assert len(documents) == 1
    document = documents[0]
    assert document.space_id == space_id
    assert document.connection_id is None
    assert document.hash
    assert len(document.hash) == 64
    row = embeddings[0]
    assert row.source_id == source_id
    assert row.artifact_id == artifact_id
    assert row.document_id == document.id
    assert row.model == "local-hash-v1"
    assert row.dimension == DIMENSION
    assert len(row.embedding) == DIMENSION
    assert len(row.content_hash) == 64
    assert row.trust == "normal"
    assert row.kind == "page"
    assert row.captured_at is not None
    assert row.url is not None

    # The same artifact does not create a second document or a second vector.
    again = await indexer.ingest_source(user_id=user_id, source_id=source_id)
    assert again.documents == 0
    assert again.embedded == 0
    assert again.deduplicated == report.chunks
    assert again.usage.texts == 0


async def test_binary_artifact_is_skipped_with_reason(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id)
    await rag.artifact(
        user_id=user_id, source_id=source_id, kind="screenshot", text=None, content_type="image/png"
    )
    report = await _indexer(sessions).ingest_source(user_id=user_id, source_id=source_id)
    assert report.documents == 0
    assert report.skipped == 1
    assert any("screenshot" in reason for reason in report.reasons)


async def test_storage_ref_html_is_read_inside_the_root(
    rag: RagSeed, sessions: SessionFactory, tmp_path: Path
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id)
    page = tmp_path / "pages" / "schedule.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        "<html><body><script>track()</script><p>Дедлайн 15 ноября</p></body></html>",
        encoding="utf-8",
    )
    await rag.artifact(
        user_id=user_id,
        source_id=source_id,
        storage_ref="pages/schedule.html",
        text=None,
        content_type="text/html",
    )
    indexer = RagIndexer(
        sessions,
        HashingEmbeddings(DIMENSION),
        RagSettings(dimension=DIMENSION, artifact_root=tmp_path),
        resolver=DefaultArtifactTextResolver(root=tmp_path),
    )
    report = await indexer.ingest_source(user_id=user_id, source_id=source_id)
    assert report.documents == 1
    async with sessions() as session:
        chunks = list(await session.scalars(select(Chunk).where(Chunk.user_id == user_id)))
    combined = " ".join(chunk.text for chunk in chunks)
    assert "Дедлайн 15 ноября" in combined
    assert "<p>" not in combined
    assert "track()" not in combined


async def test_storage_ref_escape_is_refused(
    rag: RagSeed, sessions: SessionFactory, tmp_path: Path
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id)
    outside = tmp_path.parent / "outside-rag-test.txt"
    outside.write_text("чужой файл", encoding="utf-8")
    try:
        await rag.artifact(
            user_id=user_id,
            source_id=source_id,
            storage_ref="../outside-rag-test.txt",
            text=None,
        )
        report = await _indexer(sessions).ingest_source(user_id=user_id, source_id=source_id)
    finally:
        outside.unlink(missing_ok=True)
    assert report.documents == 0
    assert report.skipped == 1


async def test_ingest_text_and_existing_document_chunks(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    indexer = _indexer(sessions)
    text_report = await indexer.ingest_text(
        user_id=user_id,
        space_id=space_id,
        title="Заметка",
        text=TEXT,
        url="https://example.test/note",
        kind="doc",
    )
    assert text_report.documents == 1
    assert text_report.embedded == text_report.chunks

    document_id = await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Старый документ",
        chunks=["Расписание пар на неделю", "Дедлайн по матанализу"],
    )
    document_report = await indexer.ingest_document(user_id=user_id, document_id=document_id)
    assert document_report.chunks == 2
    assert document_report.embedded == 2
    again = await indexer.ingest_document(user_id=user_id, document_id=document_id)
    assert again.embedded == 0
    assert again.deduplicated == 2
    missing = await indexer.ingest_document(user_id=user_id, document_id="missing-document")
    assert missing.skipped == 1
    assert missing.reasons == ("document_not_found",)


async def test_ingest_artifact_rejects_a_foreign_artifact(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    owner = await rag.user()
    stranger = await rag.user("Другой")
    owner_space = await rag.space(owner)
    owner_source = await rag.source(user_id=owner, space_id=owner_space)
    artifact_id = await rag.artifact(user_id=owner, source_id=owner_source, text=TEXT)
    report = await _indexer(sessions).ingest_artifact(user_id=stranger, artifact_id=artifact_id)
    assert report.skipped == 1
    assert report.reasons == ("artifact_not_found",)


async def test_rag_tools_are_wired_only_when_enabled(sessions: SessionFactory) -> None:
    disabled = RagSettings(enabled=False)
    assert build_rag_tools(sessions, disabled) == []
    enabled = RagSettings(enabled=True, dimension=32)
    tools = build_rag_tools(sessions, enabled)
    assert [tool.spec.name for tool in tools] == ["rag_search", "rag_ingest"]
    assert tools[0].spec.policy == "read"
    assert tools[1].spec.policy == "write_soft"
    assert tools[1].spec.idempotent is True
