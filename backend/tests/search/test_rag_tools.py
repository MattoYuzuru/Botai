"""Tool layer: argument validation, honest summaries, flag wiring (no database, fakes)."""

from __future__ import annotations

from datetime import UTC, datetime

from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.tools.rag import RagIngestTool, RagSearchTool
from botai.search.embeddings import EmbeddingUsage
from botai.search.rag import IngestReport, RagAnswer, RagHit


def _hit(title: str = "Матанализ") -> RagHit:
    return RagHit(
        title=title,
        url="https://example.test/mat",
        snippet="дедлайн 15 ноября",
        score=0.9,
        kind="page",
        chunk_id="chunk-1",
        document_id="doc-1",
        source_id="source-1",
        artifact_id="artifact-1",
        captured_at=datetime.now(UTC),
        age_days=1.0,
        stale=False,
        reasons=("fts", "vector"),
    )


class FakeService:
    def __init__(self, answer: RagAnswer) -> None:
        self.answer = answer
        self.queries: list[str] = []

    async def search(self, **kwargs: object) -> RagAnswer:
        self.queries.append(str(kwargs.get("query", "")))
        return self.answer


def _context(user_id: str | None = "user-1") -> ToolContext:
    return ToolContext(run_id="run-1", user_id=user_id)


def _answer(*hits: RagHit, mode: str = "hybrid", degraded: str | None = None) -> RagAnswer:
    return RagAnswer(
        query="дедлайн",
        results=tuple(hits),
        mode=mode,
        degraded=degraded,
        usage=EmbeddingUsage(),
    )


async def test_rag_search_success_and_summary() -> None:
    service = FakeService(_answer(_hit()))
    tool = RagSearchTool(service)  # type: ignore[arg-type]
    result = await tool.run({"query": "  дедлайн  "}, _context())
    assert result.ok
    assert service.queries == ["дедлайн"]
    assert result.data["count"] == 1
    assert result.data["results"][0]["title"] == "Матанализ"
    assert result.data["results"][0]["sourceId"] == "source-1"
    assert result.data["mode"] == "hybrid"
    assert "гибрид" in result.summary


async def test_rag_search_empty_and_degraded() -> None:
    empty = RagSearchTool(FakeService(_answer()))  # type: ignore[arg-type]
    result = await empty.run({"query": "ничего"}, _context())
    assert result.ok
    assert result.data["count"] == 0
    assert "не найдено" in result.summary

    degraded = RagSearchTool(  # type: ignore[arg-type]
        FakeService(_answer(_hit(), mode="fts_only", degraded="vector_unavailable: down"))
    )
    found = await degraded.run({"query": "дедлайн"}, _context())
    assert found.data["mode"] == "fts_only"
    assert found.data["degraded"] == "vector_unavailable: down"
    assert "FTS" in found.summary


async def test_rag_search_validation() -> None:
    tool = RagSearchTool(FakeService(_answer()))  # type: ignore[arg-type]
    missing = await tool.run({}, _context())
    assert not missing.ok
    assert missing.error is not None
    assert missing.error.code == "invalid_arguments"
    too_many = await tool.run({"query": "x", "limit": 999}, _context())
    assert not too_many.ok
    no_user = await tool.run({"query": "x"}, _context(user_id=None))
    assert not no_user.ok
    assert no_user.error is not None
    assert no_user.error.code == "no_user"


class FakeIndexer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def ingest_source(self, **kwargs: object) -> IngestReport:
        self.calls.append(("source", kwargs))
        return IngestReport(documents=1, chunks=3, embedded=3)

    async def ingest_artifact(self, **kwargs: object) -> IngestReport:
        self.calls.append(("artifact", kwargs))
        return IngestReport(chunks=1, embedded=1)

    async def ingest_text(self, **kwargs: object) -> IngestReport:
        self.calls.append(("text", kwargs))
        return IngestReport(documents=1, chunks=2, embedded=2)


async def test_rag_ingest_routes_one_target_and_reports_counts() -> None:
    indexer = FakeIndexer()
    tool = RagIngestTool(indexer)  # type: ignore[arg-type]
    source = await tool.run({"source_id": "source-1"}, _context())
    assert source.ok
    assert source.data["documents"] == 1
    assert "документов 1" in source.summary
    assert indexer.calls[-1][0] == "source"

    artifact = await tool.run({"artifact_id": "artifact-1"}, _context())
    assert artifact.ok
    assert indexer.calls[-1][0] == "artifact"

    text = await tool.run(
        {"text": "Заметка о дедлайне", "space_id": "space-1", "title": "Заметка"}, _context()
    )
    assert text.ok
    assert indexer.calls[-1][0] == "text"


async def test_rag_ingest_validation() -> None:
    tool = RagIngestTool(FakeIndexer())  # type: ignore[arg-type]
    nothing = await tool.run({}, _context())
    assert not nothing.ok
    assert nothing.error is not None
    assert nothing.error.code == "invalid_arguments"
    two_targets = await tool.run({"source_id": "s", "text": "t", "space_id": "p"}, _context())
    assert not two_targets.ok
    text_without_space = await tool.run({"text": "t"}, _context())
    assert not text_without_space.ok
    no_user = await tool.run({"source_id": "s"}, _context(user_id=None))
    assert not no_user.ok
    assert no_user.error is not None
    assert no_user.error.code == "no_user"
