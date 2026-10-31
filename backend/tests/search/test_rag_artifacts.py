"""Automatic indexing of source artifacts: the callable hook, FileArtifactStore bytes, ops command.

`reindex_source_artifacts` is what the sources runtime calls after a run and what
`python -m botai.ops.reindex_rag` runs for a backfill. Here it is exercised on fakeuni-shaped
artifacts: text metadata, bytes behind `FileArtifactStore`, a binary that must be skipped, and a
repeat that must deduplicate instead of duplicating.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from botai.db.models import Chunk, ChunkEmbedding, Document
from botai.ops import reindex_rag as ops
from botai.search.embeddings import HashingEmbeddings
from botai.search.rag import (
    RagIndexer,
    RagSettings,
    build_indexer,
    reindex_source_artifacts,
)
from botai.sources.v4.artifacts import FileArtifactStore
from tests.search.support import RagSeed, SessionFactory

DIMENSION = 384
TEXT = "Дедлайн по матанализу — 15 ноября. Сдать в личном кабинете."
JSON_TEXT = '{"deadline": "15 ноября", "course": "матанализ"}'


def _settings() -> RagSettings:
    return RagSettings(enabled=True, dimension=DIMENSION)


def test_build_indexer_is_the_hook_and_follows_the_flag(sessions: SessionFactory) -> None:
    assert build_indexer(sessions, RagSettings(enabled=False)) is None
    indexer = build_indexer(sessions, _settings())
    assert isinstance(indexer, RagIndexer)


async def test_reindex_source_artifacts_records_provenance_and_dedupes(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id, trust="high")
    page_id = await rag.artifact(user_id=user_id, source_id=source_id, kind="page", text=TEXT)
    json_id = await rag.artifact(
        user_id=user_id,
        source_id=source_id,
        kind="json",
        text=JSON_TEXT,
        content_type="application/json",
    )
    await rag.artifact(
        user_id=user_id,
        source_id=source_id,
        kind="screenshot",
        text=None,
        content_type="image/png",
    )

    report = await reindex_source_artifacts(
        sessions,
        user_id=user_id,
        source_id=source_id,
        settings=_settings(),
        provider=HashingEmbeddings(DIMENSION),
    )
    assert report.documents == 2
    assert report.chunks >= 2
    assert report.embedded == report.chunks
    assert report.skipped == 1
    assert any("screenshot" in reason for reason in report.reasons)

    async with sessions() as session:
        embeddings = list(
            await session.scalars(select(ChunkEmbedding).where(ChunkEmbedding.user_id == user_id))
        )
        documents = list(await session.scalars(select(Document).where(Document.user_id == user_id)))
    assert len(documents) == 2
    assert {row.artifact_id for row in embeddings} == {page_id, json_id}
    assert {row.source_id for row in embeddings} == {source_id}
    assert {row.trust for row in embeddings} == {"high"}
    assert {row.kind for row in embeddings} == {"page", "json"}
    assert all(row.captured_at is not None for row in embeddings)
    assert all(row.embedding_vec is not None for row in embeddings)

    again = await reindex_source_artifacts(
        sessions,
        user_id=user_id,
        source_id=source_id,
        settings=_settings(),
        provider=HashingEmbeddings(DIMENSION),
    )
    assert again.documents == 0
    assert again.embedded == 0
    assert again.deduplicated == report.chunks
    async with sessions() as session:
        same = list(await session.scalars(select(Document).where(Document.user_id == user_id)))
    assert len(same) == 2, "the same text must not create a second document"


async def test_reindex_without_source_id_is_bounded_by_max_sources(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    first = await rag.source(user_id=user_id, space_id=space_id, title="Первый")
    second = await rag.source(user_id=user_id, space_id=space_id, title="Второй")
    await rag.artifact(user_id=user_id, source_id=first, text="Материалы первого источника")
    await rag.artifact(user_id=user_id, source_id=second, text="Материалы второго источника")
    report = await reindex_source_artifacts(
        sessions,
        user_id=user_id,
        settings=_settings(),
        provider=HashingEmbeddings(DIMENSION),
        max_sources=1,
    )
    assert report.documents == 1, "one call handles at most max_sources sources"
    async with sessions() as session:
        indexed_sources = set(await session.scalars(select(ChunkEmbedding.source_id)))
    assert indexed_sources == {first}


async def test_file_artifact_store_bytes_are_indexed(
    rag: RagSeed, sessions: SessionFactory, tmp_path: Path
) -> None:
    """The bytes lane C saves are read from the store root and stripped of HTML noise."""
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id)
    store = FileArtifactStore(tmp_path)
    blob = store.put(
        b"<html><body><script>track()</script><p>Dedlayn 15 noyabrya</p></body></html>"
    )
    await rag.artifact(
        user_id=user_id,
        source_id=source_id,
        kind="page",
        text=None,
        storage_ref=str(blob.storage_ref),
        content_type="text/html",
    )
    report = await reindex_source_artifacts(
        sessions,
        user_id=user_id,
        source_id=source_id,
        artifact_root=store.root,
        settings=RagSettings(enabled=True, dimension=DIMENSION, artifact_root=store.root),
        provider=HashingEmbeddings(DIMENSION),
    )
    assert report.documents == 1
    assert report.chunks >= 1
    async with sessions() as session:
        chunks = list(await session.scalars(select(Chunk).where(Chunk.user_id == user_id)))
    combined = " ".join(chunk.text for chunk in chunks)
    assert "Dedlayn 15 noyabrya" in combined
    assert "<p>" not in combined
    assert "track()" not in combined


async def test_ops_command_indexes_artifacts_and_backfills_vectors(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    email = "reindex-ops@fakeuni.test"
    user_id = await rag.user(email=email)
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id)
    await rag.artifact(user_id=user_id, source_id=source_id, text=TEXT)
    # A row embedded before the pgvector revision: JSONB only, waiting for the backfill.
    document_id = await rag.document_with_chunks(
        user_id=user_id,
        space_id=space_id,
        title="Старый",
        chunks=["Старый текст без вектора"],
    )
    async with sessions() as session:
        chunk = await session.scalar(select(Chunk).where(Chunk.document_id == document_id))
    assert chunk is not None
    await rag.chunk_embedding(
        user_id=user_id,
        chunk_id=chunk.id,
        document_id=document_id,
        model="local-hash-v1",
        vector=[0.5] * DIMENSION,
    )

    lines: list[str] = []
    code = await ops.run(
        ["--email", email],
        sessionmaker=sessions,
        settings=_settings(),
        out=lines.append,
    )
    assert code == 0
    output = "\n".join(lines)
    assert "Артефакты:" in output
    assert "pgvector: доступен" in output
    assert "заполнено embedding_vec: 1" in output
    async with sessions() as session:
        old = await session.scalar(
            select(ChunkEmbedding).where(ChunkEmbedding.chunk_id == chunk.id)
        )
        indexed = list(
            await session.scalars(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.user_id == user_id,
                    ChunkEmbedding.artifact_id.is_not(None),
                )
            )
        )
    assert old is not None
    assert old.embedding_vec is not None
    assert indexed, "the artifact must be indexed by the command"


async def test_ops_command_reports_unknown_user(sessions: SessionFactory) -> None:
    code = await ops.run(
        ["--email", "nobody@fakeuni.test"],
        sessionmaker=sessions,
        settings=_settings(),
        out=lambda _: None,
    )
    assert code == 1
