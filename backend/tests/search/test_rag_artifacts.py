"""Automatic indexing of source artifacts: the dispatch hook, FileArtifactStore bytes, ops command.

`autoindex_source_artifacts` is what the v4 dispatch calls after a successful source run;
`reindex_source_artifacts` is the callable behind it and behind `python -m botai.ops.reindex_rag`.
Here both are exercised on fakeuni-shaped artifacts: text metadata, bytes behind
`FileArtifactStore`, a binary that must be skipped, a repeat that must deduplicate instead of
duplicating, and the flag/timeout bounds of the soft hook.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from sqlalchemy import select

from botai.db.models import Chunk, ChunkEmbedding, Document
from botai.ops import reindex_rag as ops
from botai.search import rag as rag_module
from botai.search.embeddings import HashingEmbeddings
from botai.search.rag import (
    RagSettings,
    autoindex_source_artifacts,
    reindex_source_artifacts,
)
from botai.sources.v4.artifacts import FileArtifactStore
from tests.search.support import RagSeed, SessionFactory

DIMENSION = 384
TEXT = "Дедлайн по матанализу — 15 ноября. Сдать в личном кабинете."
JSON_TEXT = '{"deadline": "15 ноября", "course": "матанализ"}'


def _settings() -> RagSettings:
    return RagSettings(enabled=True, dimension=DIMENSION)


async def test_autoindex_is_off_while_rag_is_disabled(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id)
    await rag.artifact(user_id=user_id, source_id=source_id, text=TEXT)
    report = await autoindex_source_artifacts(
        sessions,
        user_id=user_id,
        source_id=source_id,
        settings=RagSettings(enabled=False),
    )
    assert report is None
    async with sessions() as session:
        documents = list(await session.scalars(select(Document).where(Document.user_id == user_id)))
    assert documents == []


async def test_autoindex_is_bounded_and_indexes_with_provenance(
    rag: RagSeed, sessions: SessionFactory
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id, trust="high")
    first = await rag.artifact(user_id=user_id, source_id=source_id, text=TEXT)
    second = await rag.artifact(user_id=user_id, source_id=source_id, text=JSON_TEXT, kind="json")
    report = await autoindex_source_artifacts(
        sessions,
        user_id=user_id,
        source_id=source_id,
        settings=_settings(),
        limit=1,
    )
    assert report is not None
    assert report.documents == 1, "one sync pass indexes at most AUTOINDEX_LIMIT artifacts"
    async with sessions() as session:
        embeddings = list(
            await session.scalars(select(ChunkEmbedding).where(ChunkEmbedding.user_id == user_id))
        )
    assert embeddings
    assert {row.source_id for row in embeddings} == {source_id}
    assert {row.artifact_id for row in embeddings} <= {first, second}
    assert len({row.artifact_id for row in embeddings}) == 1, "only one artifact fits the budget"
    assert {row.trust for row in embeddings} == {"high"}


async def test_autoindex_times_out_softly_instead_of_hanging_a_sync(
    rag: RagSeed,
    sessions: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id)

    async def slow(*_: object, **__: object) -> object:
        await asyncio.sleep(5)
        raise AssertionError("the timeout must fire before this returns")

    monkeypatch.setattr(rag_module, "reindex_source_artifacts", slow)
    with caplog.at_level(logging.WARNING, logger="botai.search.rag"):
        report = await autoindex_source_artifacts(
            sessions,
            user_id=user_id,
            source_id=source_id,
            settings=_settings(),
            timeout_s=0.05,
        )
    assert report is None, "a timeout is a warning for the job, not an exception"
    warnings = [record for record in caplog.records if record.name == "botai.search.rag"]
    assert warnings
    assert "TimeoutError" in warnings[0].getMessage()


async def test_autoindex_repeat_adds_no_rows(rag: RagSeed, sessions: SessionFactory) -> None:
    user_id = await rag.user()
    space_id = await rag.space(user_id)
    source_id = await rag.source(user_id=user_id, space_id=space_id)
    await rag.artifact(user_id=user_id, source_id=source_id, text=TEXT)
    first = await autoindex_source_artifacts(
        sessions, user_id=user_id, source_id=source_id, settings=_settings()
    )
    again = await autoindex_source_artifacts(
        sessions, user_id=user_id, source_id=source_id, settings=_settings()
    )
    assert first is not None
    assert again is not None
    assert first.documents >= 1
    assert again.documents == 0
    assert again.embedded == 0
    async with sessions() as session:
        documents = list(await session.scalars(select(Document).where(Document.user_id == user_id)))
        embeddings = list(
            await session.scalars(select(ChunkEmbedding).where(ChunkEmbedding.user_id == user_id))
        )
    assert len(documents) == first.documents
    assert len(embeddings) == first.chunks


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
