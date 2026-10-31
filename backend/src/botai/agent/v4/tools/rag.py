"""`rag_search` / `rag_ingest`: RAG tools for the v4 runtime (plan 2.5, architecture §4.4).

The tools are thin: validation and a secret-free summary around `botai.search.rag`. The heavy
lifting (embedding provider, chunking, provenance, hybrid ranking) lives in `botai.search`, so the
same code serves the future REST endpoint and background jobs.

Wiring is the runtime's job: `build_rag_tools` returns an empty list while `BOTAI_RAG_ENABLED` is
off, and the runtime registers the returned handlers next to `build_basic_tools()`. No file of the
runtime package needs to change for that — this module only imports its public protocol.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from botai.agent.v4.protocol import ToolContext, ToolHandler, ToolResult, ToolSpec, object_schema
from botai.search.rag import (
    MAX_ARTIFACTS_PER_CALL,
    RagAnswer,
    RagIndexer,
    RagQueryService,
    RagSettings,
    rag_settings,
)

DEFAULT_LIMIT = 8
MAX_LIMIT = 20


def _invalid(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"Некорректные аргументы: {location}: {first.get('msg', 'ошибка')}"


class RagSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    kinds: list[str] = Field(default_factory=list, max_length=10)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    fresh_after: datetime | None = None
    rerank: bool = True


class RagIngestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str | None = Field(default=None, max_length=26)
    artifact_id: str | None = Field(default=None, max_length=26)
    text: str | None = Field(default=None, max_length=500_000)
    space_id: str | None = Field(default=None, max_length=26)
    title: str | None = Field(default=None, max_length=300)
    url: str | None = Field(default=None, max_length=4000)
    kind: str = Field(default="page", max_length=20)
    limit: int = Field(default=MAX_ARTIFACTS_PER_CALL, ge=1, le=MAX_ARTIFACTS_PER_CALL)

    def target(self) -> str:
        selected = [
            name
            for name, value in (
                ("source_id", self.source_id),
                ("artifact_id", self.artifact_id),
                ("text", self.text),
            )
            if value
        ]
        return selected[0] if len(selected) == 1 else ""


class RagSearchTool:
    """Answer «что у меня сказано про …» with citations, provenance and freshness."""

    spec = ToolSpec(
        name="rag_search",
        description=(
            "Найти в материалах студента (прочитанные страницы, артефакты источников, документы) "
            "фрагменты по смыслу: гибридный полнотекстовый и векторный поиск, свежесть и "
            "происхождение в каждом результате. Возвращает цитаты со ссылками, баллом, датой "
            "получения и признаком устаревания. Только чтение."
        ),
        parameters=object_schema(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 8},
                "kinds": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "maxLength": 20},
                    "description": "Вид материала: page, json, file, doc, log или вид документа.",
                },
                "source_ids": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {"type": "string", "maxLength": 26},
                    "description": "Ограничить поиск конкретными источниками (v4 source id).",
                },
                "fresh_after": {
                    "type": ["string", "null"],
                    "description": "ISO 8601: не показывать материалы старше этой даты.",
                },
                "rerank": {"type": "boolean", "default": True},
            },
            required=["query"],
        ),
        timeout_s=30.0,
    )

    def __init__(self, service: RagQueryService) -> None:
        self._service = service

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        started = time.monotonic()
        try:
            args = RagSearchArgs.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult.failure("invalid_arguments", _invalid(exc))
        if context.user_id is None:
            return ToolResult.failure("no_user", "RAG-поиск без пользователя невозможен.")
        query = args.query.strip()
        if not query:
            return ToolResult.failure("invalid_arguments", "query не может быть пустым.")
        answer: RagAnswer = await self._service.search(
            user_id=context.user_id,
            query=query,
            limit=args.limit,
            kinds=args.kinds,
            source_ids=args.source_ids,
            fresh_after=_as_utc(args.fresh_after),
            rerank=args.rerank,
        )
        results = [
            {
                "title": hit.title,
                "url": hit.url,
                "snippet": hit.snippet,
                "score": hit.score,
                "kind": hit.kind,
                "sourceId": hit.source_id,
                "artifactId": hit.artifact_id,
                "documentId": hit.document_id,
                "capturedAt": hit.captured_at.isoformat() if hit.captured_at else None,
                "ageDays": hit.age_days,
                "stale": hit.stale,
                "reasons": list(hit.reasons),
            }
            for hit in answer.results
        ]
        data: dict[str, Any] = {
            "query": query,
            "mode": answer.mode,
            "degraded": answer.degraded,
            "count": len(results),
            "results": results,
            "usage": {
                "calls": answer.usage.calls,
                "texts": answer.usage.texts,
                "cacheHits": answer.usage.cache_hits,
                "costUsd": answer.usage.cost_usd,
            },
        }
        if not results:
            reason = (
                "векторная часть недоступна, полнотекстовый поиск ничего не нашёл"
                if answer.degraded
                else "в материалах студента ничего не найдено"
            )
            return ToolResult.success(
                summary=f"RAG: {reason}.",
                data=data,
                cost_ms=int((time.monotonic() - started) * 1000),
            )
        summary = (
            f"RAG: найдено {len(results)} фрагментов "
            f"({'гибрид' if answer.mode == 'hybrid' else 'только FTS'}"
            + (f", деградация: {answer.degraded}" if answer.degraded else "")
            + f"); лучший балл {results[0]['score']}."
        )
        return ToolResult.success(
            summary=summary,
            data=data,
            cost_ms=int((time.monotonic() - started) * 1000),
        )


class RagIngestTool:
    """Index a source, one artifact or plain text into the RAG store."""

    spec = ToolSpec(
        name="rag_ingest",
        description=(
            "Проиндексировать материалы для RAG: все текстовые артефакты источника (source_id), "
            "один артефакт (artifact_id) или свободный текст (text + space_id). Идемпотентно: "
            "повторный вызов по тому же тексту ничего не дублирует. Возвращает счётчики и стоимость."
        ),
        parameters=object_schema(
            {
                "source_id": {"type": ["string", "null"], "maxLength": 26},
                "artifact_id": {"type": ["string", "null"], "maxLength": 26},
                "text": {"type": ["string", "null"], "maxLength": 500_000},
                "space_id": {"type": ["string", "null"], "maxLength": 26},
                "title": {"type": ["string", "null"], "maxLength": 300},
                "url": {"type": ["string", "null"], "maxLength": 4000},
                "kind": {"type": "string", "maxLength": 20, "default": "page"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_ARTIFACTS_PER_CALL,
                    "default": MAX_ARTIFACTS_PER_CALL,
                },
            },
        ),
        timeout_s=120.0,
        policy="write_soft",
        idempotent=True,
    )

    def __init__(self, indexer: RagIndexer) -> None:
        self._indexer = indexer

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        started = time.monotonic()
        try:
            args = RagIngestArgs.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult.failure("invalid_arguments", _invalid(exc))
        if context.user_id is None:
            return ToolResult.failure("no_user", "RAG-индексация без пользователя невозможна.")
        target = args.target()
        if not target:
            return ToolResult.failure(
                "invalid_arguments",
                "Нужен ровно один из source_id, artifact_id или text.",
            )
        if target == "source_id" and args.source_id is not None:
            report = await self._indexer.ingest_source(
                user_id=context.user_id, source_id=args.source_id, limit=args.limit
            )
        elif target == "artifact_id" and args.artifact_id is not None:
            report = await self._indexer.ingest_artifact(
                user_id=context.user_id, artifact_id=args.artifact_id
            )
        else:
            if args.text is None or args.space_id is None:
                return ToolResult.failure(
                    "invalid_arguments", "Для text нужен space_id (и непустой text)."
                )
            report = await self._indexer.ingest_text(
                user_id=context.user_id,
                space_id=args.space_id,
                title=(args.title or "Текст").strip() or "Текст",
                text=args.text,
                url=args.url,
                kind=args.kind,
            )
        data: dict[str, Any] = {
            "target": target,
            "documents": report.documents,
            "chunks": report.chunks,
            "embedded": report.embedded,
            "deduplicated": report.deduplicated,
            "skipped": report.skipped,
            "reasons": list(report.reasons),
            "usage": {
                "calls": report.usage.calls,
                "texts": report.usage.texts,
                "cacheHits": report.usage.cache_hits,
                "costUsd": report.usage.cost_usd,
            },
        }
        summary = (
            f"RAG-индексация: документов {report.documents}, чанков {report.chunks}, "
            f"новых векторов {report.embedded}, без изменений {report.deduplicated}, "
            f"пропущено {report.skipped}."
        )
        if report.skipped and not report.embedded:
            return ToolResult.success(
                summary=summary, data=data, cost_ms=int((time.monotonic() - started) * 1000)
            )
        return ToolResult.success(
            summary=summary, data=data, cost_ms=int((time.monotonic() - started) * 1000)
        )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def build_rag_tools(
    sessions: Any,
    settings: RagSettings | None = None,
) -> list[ToolHandler]:
    """The phase-2.5 tool set, or nothing while the feature flag is off.

    `sessions` is an `async_sessionmaker[AsyncSession]`; keeping it `Any` here avoids importing the
    database layer into the protocol package. The runtime calls this when it builds its tool list.
    """
    resolved = settings or rag_settings()
    if not resolved.enabled:
        return []
    provider = resolved.build_provider()
    indexer = RagIndexer(sessions, provider, resolved)
    service = RagQueryService(sessions, provider, resolved)
    return [RagSearchTool(service), RagIngestTool(indexer)]
