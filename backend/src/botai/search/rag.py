"""RAG over v4 source artifacts and v3 documents: chunking, ingestion, query, feature flag.

What is indexed
---------------
The unit of indexing is a `Document` + its `Chunk` rows (the tables v3 FTS already uses), and every
chunk gets *provenance* in `chunk_embeddings`: which source and artifact it came from, its url,
artifact kind, capture time and source trust. Text comes from two places:

* a source artifact — through an injectable `ArtifactTextResolver`. The default resolver reads
  `storage_ref` inside a configured root (the artifact store is outside the database), accepts
  `metadata["text"]` when the runner already extracted it, strips HTML tags and pretty-prints JSON;
  binary kinds (screenshot, har) are skipped with a reason, never half-indexed;
* an existing v3 document (`ingest_document`) — its chunks are already split, so only embeddings
  and provenance are added.

Idempotency: the document hash is the content address. Re-ingesting the same text skips the work
(`deduplicated`), and ingesting changed text creates a new document so the old snapshot stays
readable with its own freshness; replacing old snapshots is a retention decision, not a silent
delete here.

Vectors are written twice by design: `embedding` (JSONB, canonical and portable) always, and
`embedding_vec` (pgvector, derived) when the migrated column exists with the provider's width. A
reindex of already embedded rows fills the derived column from JSONB (`vectors_filled:<n>`), so
enabling pgvector never requires re-embedding the corpus (search/pgvector.py).

Automatic indexing: `reindex_source_artifacts` (and `build_indexer` for a future hook) is the
callable a sources runtime could use after a run, but it is deliberately **not wired** into the
runner this round — that package is owned by another lane and the phase closes without the inline
hook. The supported path for artifacts that already exist is the ops command
`python -m botai.ops.reindex_rag` (bounded, idempotent); automatic indexing after a run remains an
honest gap.

Query
-----
`RagQueryService.search` embeds the question, runs `botai.search.hybrid.hybrid_search` (FTS +
vector, fusion, freshness, provenance) and returns citations. A failed embedding provider degrades
to FTS-only and says so (`mode="fts_only"`, `degraded=...`); it never hides text that FTS found.
`vector_backend` says which physical vector leg ran (`pgvector`/`jsonb`/`none`).

Feature flag
------------
`BOTAI_RAG_ENABLED` (default off) gates wiring — the tool factory returns nothing when it is off.
The library classes below work regardless of the flag, so tests and future jobs can construct them
directly. Settings live here instead of `botai.settings.Settings` because phase 2.5 is being built
next to other in-flight v4 work; moving them into `Settings` later is a one-file change.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.db.models import Chunk, ChunkEmbedding, Document, Source, SourceArtifact
from botai.search.embeddings import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_DIMENSION,
    ApiEmbeddings,
    CachedEmbeddings,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingUsage,
    HashingEmbeddings,
    content_hash,
)
from botai.search.hybrid import HybridFilters, HybridHit, HybridSearchResult, hybrid_search
from botai.search.pgvector import backfill_vectors, state_for
from botai.sources.v4 import get_source, list_artifacts, list_sources

DEFAULT_CHUNK_CHARS = 1200
DEFAULT_CHUNK_OVERLAP = 150
DEFAULT_SCAN_LIMIT = 500
MAX_ARTIFACT_BYTES = 2_000_000
MAX_CHUNKS_PER_UNIT = 200
MAX_ARTIFACTS_PER_CALL = 100
SNIPPET_CHARS = 600
TEXT_ARTIFACT_KINDS = frozenset({"page", "json", "file", "log", "doc", "other"})
_HTML_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_WHITESPACE = re.compile(r"[ \t]+")

SessionFactory = async_sessionmaker[AsyncSession]


# --- settings and feature flag -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RagSettings:
    """Everything the RAG path needs, read from `BOTAI_RAG_*` environment variables.

    Defaults are safe and offline: disabled, local hashing embeddings, no keys, no network.
    """

    enabled: bool = False
    provider: Literal["local", "api"] = "local"
    dimension: int = DEFAULT_DIMENSION
    batch_size: int = DEFAULT_BATCH_SIZE
    chunk_chars: int = DEFAULT_CHUNK_CHARS
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    vector_scan_limit: int = DEFAULT_SCAN_LIMIT
    freshness_half_life_days: float = 30.0
    stale_after_days: float = 30.0
    api_endpoint: str = ""
    api_model: str = "text-embedding-3-small"
    api_key_env: str = "BOTAI_EMBEDDING_API_KEY"
    artifact_root: Path | None = None

    def build_provider(self) -> EmbeddingProvider:
        """The configured provider, wrapped in the process cache. No key is read here."""
        if self.provider == "api":
            if not self.api_endpoint:
                raise EmbeddingError(
                    "BOTAI_RAG_API_ENDPOINT is not configured for the API provider"
                )
            return CachedEmbeddings(
                ApiEmbeddings(
                    endpoint=self.api_endpoint,
                    model=self.api_model,
                    api_key_env=self.api_key_env,
                    dimension=self.dimension,
                    batch_size=self.batch_size,
                )
            )
        return CachedEmbeddings(HashingEmbeddings(self.dimension))

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> RagSettings:
        values = environ if environ is not None else dict(os.environ)

        def flag(name: str, default: bool) -> bool:
            raw = values.get(name)
            if raw is None:
                return default
            return raw.strip().casefold() in {"1", "true", "yes", "on"}

        def number(name: str, default: float) -> float:
            try:
                return float(values[name])
            except (KeyError, ValueError):
                return default

        artifact_root = values.get("BOTAI_RAG_ARTIFACT_ROOT", "").strip()
        return cls(
            enabled=flag("BOTAI_RAG_ENABLED", False),
            provider="api" if values.get("BOTAI_RAG_PROVIDER", "local") == "api" else "local",
            dimension=int(number("BOTAI_RAG_DIMENSION", DEFAULT_DIMENSION)),
            batch_size=int(number("BOTAI_RAG_BATCH_SIZE", DEFAULT_BATCH_SIZE)),
            chunk_chars=int(number("BOTAI_RAG_CHUNK_CHARS", DEFAULT_CHUNK_CHARS)),
            chunk_overlap=int(number("BOTAI_RAG_CHUNK_OVERLAP", DEFAULT_CHUNK_OVERLAP)),
            vector_scan_limit=int(number("BOTAI_RAG_VECTOR_SCAN_LIMIT", DEFAULT_SCAN_LIMIT)),
            freshness_half_life_days=number("BOTAI_RAG_HALF_LIFE_DAYS", 30.0),
            stale_after_days=number("BOTAI_RAG_STALE_AFTER_DAYS", 30.0),
            api_endpoint=values.get("BOTAI_RAG_API_ENDPOINT", "").strip(),
            api_model=values.get("BOTAI_RAG_API_MODEL", "text-embedding-3-small").strip(),
            api_key_env=values.get("BOTAI_RAG_API_KEY_ENV", "BOTAI_EMBEDDING_API_KEY").strip(),
            artifact_root=Path(artifact_root) if artifact_root else None,
        )


def rag_settings() -> RagSettings:
    """Current settings from the environment; the provider is built lazily by the caller."""
    return RagSettings.from_env()


def rag_enabled() -> bool:
    """The single feature flag: tools are wired only when this is true."""
    return rag_settings().enabled


# --- chunking ------------------------------------------------------------------------------------


def split_text(
    text: str,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split clean text into overlapping chunks on paragraph/sentence/space boundaries."""
    cleaned = _WHITESPACE.sub(" ", text.replace("\r\n", "\n")).strip()
    if not cleaned:
        return []
    chunk_chars = max(200, chunk_chars)
    overlap = max(0, min(overlap, chunk_chars // 2))
    if len(cleaned) <= chunk_chars:
        return [cleaned]
    chunks: list[str] = []
    start = 0
    while start < len(cleaned) and len(chunks) < MAX_CHUNKS_PER_UNIT:
        end = min(start + chunk_chars, len(cleaned))
        if end < len(cleaned):
            window = cleaned[start:end]
            candidates = [
                window.rfind("\n\n"),
                window.rfind("\n"),
                window.rfind(". "),
                window.rfind(" "),
            ]
            cut = max(candidates)
            if cut >= chunk_chars // 3:
                end = start + cut + 1
        piece = cleaned[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return chunks


# --- artifact text -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactText:
    """Text extracted from one artifact, with the provenance the index needs."""

    artifact_id: str
    source_id: str
    sha256: str
    kind: str
    title: str
    url: str | None
    content_type: str | None
    captured_at: datetime | None
    trust: str | None
    text: str


class ArtifactTextResolver(Protocol):
    """Where the text of an artifact comes from; tests inject a scripted resolver."""

    async def resolve(self, artifact: SourceArtifact, *, source: Source) -> ArtifactText | None: ...


def _strip_html(raw: str) -> str:
    without_scripts = _SCRIPT.sub(" ", raw)
    text = _HTML_TAG.sub(" ", without_scripts)
    return _WHITESPACE.sub(" ", text).strip()


def _decode_bytes(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


@dataclass(slots=True)
class DefaultArtifactTextResolver:
    """Reads `storage_ref` under a configured root, else `metadata["text"]`, else skips.

    The artifact store is deliberately outside the database (docs/v4/14-sources-backend.md); the
    root is configuration, and a path that escapes it is refused rather than indexed.
    """

    root: Path | None = None
    max_bytes: int = MAX_ARTIFACT_BYTES

    async def resolve(self, artifact: SourceArtifact, *, source: Source) -> ArtifactText | None:
        if artifact.kind not in TEXT_ARTIFACT_KINDS:
            return None
        text = self._metadata_text(artifact)
        if text is None:
            text = self._storage_text(artifact)
        if text is None:
            return None
        if (artifact.content_type and "html" in artifact.content_type) or text.lstrip().startswith(
            "<"
        ):
            text = _strip_html(text)
        if artifact.kind == "json":
            text = self._pretty_json(text)
        text = text.strip()
        if not text:
            return None
        title = (
            artifact.url or str(artifact.metadata_json.get("title") or "") or artifact.sha256[:12]
        )
        return ArtifactText(
            artifact_id=artifact.id,
            source_id=source.id,
            sha256=artifact.sha256,
            kind=artifact.kind,
            title=title[:300],
            url=artifact.url,
            content_type=artifact.content_type,
            captured_at=artifact.captured_at,
            trust=source.trust,
            text=text[: self.max_bytes],
        )

    @staticmethod
    def _metadata_text(artifact: SourceArtifact) -> str | None:
        value = artifact.metadata_json.get("text")
        return value if isinstance(value, str) and value.strip() else None

    def _storage_text(self, artifact: SourceArtifact) -> str | None:
        if not artifact.storage_ref or self.root is None:
            return None
        root = self.root.resolve()
        candidate = (self.root / artifact.storage_ref).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        try:
            size = candidate.stat().st_size
        except OSError:
            return None
        if size > self.max_bytes:
            return None
        try:
            return _decode_bytes(candidate.read_bytes())
        except OSError:
            return None

    @staticmethod
    def _pretty_json(text: str) -> str:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
        return json.dumps(payload, ensure_ascii=False, indent=2)


# --- ingestion -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestReport:
    """What one ingest call did; `reasons` explains skips without leaking content."""

    documents: int = 0
    chunks: int = 0
    embedded: int = 0
    deduplicated: int = 0
    skipped: int = 0
    usage: EmbeddingUsage = field(default_factory=EmbeddingUsage)
    reasons: tuple[str, ...] = ()


def _document_kind(artifact_kind: str) -> str:
    """`documents.kind` is the v3 vocabulary: page | file | sheet."""
    if artifact_kind in {"page", "json"}:
        return "page"
    if artifact_kind == "file" or artifact_kind == "doc":
        return "file"
    return "page"


class RagIndexer:
    """Indexes source artifacts and documents into chunks + embeddings + provenance."""

    def __init__(
        self,
        sessions: SessionFactory,
        provider: EmbeddingProvider,
        settings: RagSettings,
        resolver: ArtifactTextResolver | None = None,
    ) -> None:
        self._sessions = sessions
        self._provider = provider
        self._settings = settings
        self._resolver: ArtifactTextResolver = resolver or DefaultArtifactTextResolver(
            root=settings.artifact_root
        )

    async def ingest_source(
        self,
        *,
        user_id: str,
        source_id: str,
        kinds: Sequence[str] | None = None,
        limit: int = MAX_ARTIFACTS_PER_CALL,
    ) -> IngestReport:
        """Index text artifacts of one source; binary artifacts are skipped with a reason."""
        limit = max(1, min(limit, MAX_ARTIFACTS_PER_CALL))
        async with self._sessions() as session:
            source = await get_source(session, user_id=user_id, source_id=source_id)
            wanted = set(kinds) if kinds else None
            report = _merge_reports([])
            usage_before = self._provider.usage()
            for artifact in await list_artifacts(
                session, user_id=user_id, source_id=source_id, limit=limit
            ):
                if wanted is not None and artifact.kind not in wanted:
                    continue
                text = await self._resolver.resolve(artifact, source=source)
                if text is None:
                    report = _merge_reports(
                        [report, IngestReport(skipped=1, reasons=(f"artifact:{artifact.kind}",))]
                    )
                    continue
                outcome = await self._index_unit(
                    session,
                    user_id=user_id,
                    space_id=source.space_id,
                    title=text.title,
                    text=text.text,
                    url=text.url,
                    document_kind=_document_kind(text.kind),
                    provenance_kind=text.kind,
                    fetched_at=text.captured_at or datetime.now(UTC),
                    source_id=source.id,
                    artifact_id=text.artifact_id,
                    trust=text.trust,
                )
                report = _merge_reports([report, outcome])
            usage = self._provider.usage().minus(usage_before)
            await session.commit()
        return IngestReport(
            documents=report.documents,
            chunks=report.chunks,
            embedded=report.embedded,
            deduplicated=report.deduplicated,
            skipped=report.skipped,
            usage=usage,
            reasons=report.reasons,
        )

    async def ingest_artifact(self, *, user_id: str, artifact_id: str) -> IngestReport:
        """Index exactly one artifact (the runner knows its id after saving it)."""
        async with self._sessions() as session:
            artifact = await session.get(SourceArtifact, artifact_id)
            if artifact is None or artifact.user_id != user_id:
                return IngestReport(skipped=1, reasons=("artifact_not_found",))
            source = await get_source(session, user_id=user_id, source_id=artifact.source_id)
            usage_before = self._provider.usage()
            text = await self._resolver.resolve(artifact, source=source)
            if text is None:
                await session.rollback()
                return IngestReport(skipped=1, reasons=(f"not_text:{artifact.kind}",))
            outcome = await self._index_unit(
                session,
                user_id=user_id,
                space_id=source.space_id,
                title=text.title,
                text=text.text,
                url=text.url,
                document_kind=_document_kind(text.kind),
                provenance_kind=text.kind,
                fetched_at=text.captured_at or datetime.now(UTC),
                source_id=source.id,
                artifact_id=text.artifact_id,
                trust=text.trust,
            )
            usage = self._provider.usage().minus(usage_before)
            await session.commit()
        return IngestReport(
            documents=outcome.documents,
            chunks=outcome.chunks,
            embedded=outcome.embedded,
            deduplicated=outcome.deduplicated,
            skipped=outcome.skipped,
            usage=usage,
            reasons=outcome.reasons,
        )

    async def ingest_text(
        self,
        *,
        user_id: str,
        space_id: str,
        title: str,
        text: str,
        url: str | None = None,
        kind: str = "page",
        source_id: str | None = None,
        captured_at: datetime | None = None,
    ) -> IngestReport:
        """Index free text (a chat export, a page the agent already holds) without an artifact."""
        async with self._sessions() as session:
            usage_before = self._provider.usage()
            outcome = await self._index_unit(
                session,
                user_id=user_id,
                space_id=space_id,
                title=title,
                text=text,
                url=url,
                document_kind=_document_kind(kind),
                provenance_kind=kind,
                fetched_at=captured_at or datetime.now(UTC),
                source_id=source_id,
                artifact_id=None,
                trust=None,
            )
            usage = self._provider.usage().minus(usage_before)
            await session.commit()
        return IngestReport(
            documents=outcome.documents,
            chunks=outcome.chunks,
            embedded=outcome.embedded,
            deduplicated=outcome.deduplicated,
            skipped=outcome.skipped,
            usage=usage,
            reasons=outcome.reasons,
        )

    async def ingest_document(self, *, user_id: str, document_id: str) -> IngestReport:
        """Embed chunks of an existing v3 document (no re-splitting, no new document row)."""
        async with self._sessions() as session:
            document = await session.get(Document, document_id)
            if document is None or document.user_id != user_id:
                return IngestReport(skipped=1, reasons=("document_not_found",))
            chunks = list(
                await session.scalars(
                    select(Chunk)
                    .where(Chunk.user_id == user_id, Chunk.document_id == document_id)
                    .order_by(Chunk.ord)
                )
            )
            if not chunks:
                return IngestReport(skipped=1, reasons=("no_chunks",))
            usage_before = self._provider.usage()
            report = await self._embed_chunks(
                session,
                user_id=user_id,
                chunks=chunks,
                source_id=None,
                artifact_id=None,
                title=document.title,
                url=document.url,
                provenance_kind=document.kind,
                fetched_at=document.fetched_at,
                trust=None,
            )
            usage = self._provider.usage().minus(usage_before)
            await session.commit()
        return IngestReport(
            chunks=report.chunks,
            embedded=report.embedded,
            deduplicated=report.deduplicated,
            usage=usage,
            reasons=report.reasons,
        )

    async def _index_unit(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        space_id: str,
        title: str,
        text: str,
        url: str | None,
        document_kind: str,
        provenance_kind: str,
        fetched_at: datetime,
        source_id: str | None,
        artifact_id: str | None,
        trust: str | None,
    ) -> IngestReport:
        digest = content_hash(text)
        existing = await session.scalar(
            select(Document).where(Document.user_id == user_id, Document.hash == digest)
        )
        if existing is not None:
            chunks = list(
                await session.scalars(
                    select(Chunk)
                    .where(Chunk.user_id == user_id, Chunk.document_id == existing.id)
                    .order_by(Chunk.ord)
                )
            )
            return await self._embed_chunks(
                session,
                user_id=user_id,
                chunks=chunks,
                source_id=source_id,
                artifact_id=artifact_id,
                title=existing.title,
                url=existing.url,
                provenance_kind=provenance_kind,
                fetched_at=existing.fetched_at,
                trust=trust,
            )
        pieces = split_text(
            text, chunk_chars=self._settings.chunk_chars, overlap=self._settings.chunk_overlap
        )
        if not pieces:
            return IngestReport(skipped=1, reasons=("empty_text",))
        document = Document(
            user_id=user_id,
            space_id=space_id,
            connection_id=None,
            kind=document_kind,
            title=title[:300],
            url=url,
            mime=None,
            fetched_at=fetched_at,
            hash=digest,
        )
        session.add(document)
        await session.flush()
        rows: list[Chunk] = []
        for order, piece in enumerate(pieces):
            row = Chunk(
                user_id=user_id,
                document_id=document.id,
                ord=order,
                text=piece,
                tokens=len(piece) // 3,
            )
            session.add(row)
            rows.append(row)
        await session.flush()
        report = await self._embed_chunks(
            session,
            user_id=user_id,
            chunks=rows,
            source_id=source_id,
            artifact_id=artifact_id,
            title=title[:300],
            url=url,
            provenance_kind=provenance_kind,
            fetched_at=fetched_at,
            trust=trust,
        )
        return IngestReport(
            documents=1,
            chunks=report.chunks,
            embedded=report.embedded,
            deduplicated=report.deduplicated,
            skipped=report.skipped,
            reasons=report.reasons,
        )

    async def _embed_chunks(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        chunks: Sequence[Chunk],
        source_id: str | None,
        artifact_id: str | None,
        title: str,
        url: str | None,
        provenance_kind: str,
        fetched_at: datetime,
        trust: str | None,
    ) -> IngestReport:
        if not chunks:
            return IngestReport(skipped=1, reasons=("no_chunks",))
        model = self._provider.name
        vector_state = await state_for(session)
        already = set(
            await session.scalars(
                select(ChunkEmbedding.chunk_id).where(
                    ChunkEmbedding.user_id == user_id,
                    ChunkEmbedding.model == model,
                    ChunkEmbedding.chunk_id.in_([chunk.id for chunk in chunks]),
                )
            )
        )
        pending = [chunk for chunk in chunks if chunk.id not in already]
        if not pending:
            reasons = ["already_indexed"]
            reasons.extend(await self._fill_vectors(session, user_id, model, chunks))
            return IngestReport(
                chunks=len(chunks),
                deduplicated=len(chunks),
                reasons=tuple(reasons),
            )
        vectors = await self._provider.embed([chunk.text for chunk in pending])
        for chunk, vector in zip(pending, vectors, strict=True):
            row = ChunkEmbedding(
                user_id=user_id,
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                source_id=source_id,
                artifact_id=artifact_id,
                model=model,
                dimension=len(vector),
                content_hash=content_hash(chunk.text),
                embedding=vector,
                url=url,
                title=title,
                kind=provenance_kind,
                captured_at=fetched_at,
                trust=trust,
            )
            if vector_state.available and vector_state.dimension == len(vector):
                row.embedding_vec = vector
            session.add(row)
        await session.flush()
        reasons = await self._fill_vectors(
            session, user_id, model, [chunk for chunk in chunks if chunk.id in already]
        )
        return IngestReport(
            chunks=len(chunks),
            embedded=len(pending),
            deduplicated=len(already),
            reasons=tuple(reasons),
        )

    async def _fill_vectors(
        self,
        session: AsyncSession,
        user_id: str,
        model: str,
        chunks: Sequence[Chunk],
    ) -> list[str]:
        """Derive `embedding_vec` for rows embedded before the pgvector column existed.

        Cheap (JSONB is already in the database), bounded by the unit's chunk count, and skipped
        silently while pgvector is unavailable — the JSONB path is the answer in that case.
        """
        if not chunks:
            return []
        report = await backfill_vectors(
            session,
            user_id=user_id,
            model=model,
            chunk_ids=[chunk.id for chunk in chunks],
            max_rows=len(chunks),
        )
        return [f"vectors_filled:{report.filled}"] if report.filled else []


def _merge_reports(reports: Sequence[IngestReport]) -> IngestReport:
    return IngestReport(
        documents=sum(report.documents for report in reports),
        chunks=sum(report.chunks for report in reports),
        embedded=sum(report.embedded for report in reports),
        deduplicated=sum(report.deduplicated for report in reports),
        skipped=sum(report.skipped for report in reports),
        reasons=tuple(reason for report in reports for reason in report.reasons),
    )


# --- automatic indexing of source artifacts (the hook point for the sources runtime) -------------


def build_indexer(
    sessions: SessionFactory,
    settings: RagSettings | None = None,
) -> RagIndexer | None:
    """Factory for a future post-run hook; `None` while `BOTAI_RAG_ENABLED` is off.

    NOT wired into the sources runtime in this round (that package is owned by another lane). A
    future hook builds it once per runtime, then per finished run calls
    `await indexer.ingest_source(user_id=..., source_id=...)`; work is idempotent, so a retry only
    fills what is missing. Until such a hook exists, `botai.ops.reindex_rag` is the supported path.
    """
    resolved = settings or rag_settings()
    if not resolved.enabled:
        return None
    return RagIndexer(sessions, resolved.build_provider(), resolved)


async def reindex_source_artifacts(
    sessions: SessionFactory,
    *,
    user_id: str,
    source_id: str | None = None,
    limit: int = MAX_ARTIFACTS_PER_CALL,
    max_sources: int = 50,
    artifact_root: Path | None = None,
    settings: RagSettings | None = None,
    provider: EmbeddingProvider | None = None,
) -> IngestReport:
    """Index the artifacts of one source — or of every source of the user — into the RAG store.

    The callable behind the ops backfill (and a future automatic hook). It reads lane C's artifacts
    (`list_artifacts`), resolves text from `metadata["text"]` or from `storage_ref` under
    `artifact_root` (the `FileArtifactStore` root; default `BOTAI_RAG_ARTIFACT_ROOT`), and writes
    documents/chunks/embeddings with provenance. Dedupe: the document `content_hash` (the same text
    is one document) plus the `(chunk_id, model)` embedding key; a binary artifact is skipped with
    `artifact:<kind>` in `reasons`, never half-indexed. One call is bounded by `limit` artifacts per
    source and `max_sources` sources, so a job step cannot run unbounded work.
    """
    resolved = settings or rag_settings()
    embedder = provider or resolved.build_provider()
    resolver = DefaultArtifactTextResolver(root=artifact_root) if artifact_root else None
    indexer = RagIndexer(sessions, embedder, resolved, resolver=resolver)
    usage_before = embedder.usage()
    if source_id is not None:
        report = await indexer.ingest_source(user_id=user_id, source_id=source_id, limit=limit)
    else:
        async with sessions() as session:
            selected = [source.id for source in await list_sources(session, user_id=user_id)]
        reports = [
            await indexer.ingest_source(user_id=user_id, source_id=current, limit=limit)
            for current in selected[: max(1, max_sources)]
        ]
        report = _merge_reports(reports)
    return IngestReport(
        documents=report.documents,
        chunks=report.chunks,
        embedded=report.embedded,
        deduplicated=report.deduplicated,
        skipped=report.skipped,
        usage=embedder.usage().minus(usage_before),
        reasons=report.reasons,
    )


# --- query ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RagHit:
    """One citation for the model and the UI: where the text came from and how fresh it is."""

    title: str
    url: str | None
    snippet: str
    score: float
    kind: str
    chunk_id: str
    document_id: str
    source_id: str | None
    artifact_id: str | None
    captured_at: datetime | None
    age_days: float | None
    stale: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RagAnswer:
    query: str
    results: tuple[RagHit, ...]
    mode: str
    degraded: str | None
    usage: EmbeddingUsage
    # "pgvector" | "jsonb" | "none": which physical vector leg ran; kept out of the tool payload.
    vector_backend: str = "none"


class RagQueryService:
    """Question → citations. Embeds the query and delegates ranking to `hybrid_search`."""

    def __init__(
        self,
        sessions: SessionFactory,
        provider: EmbeddingProvider,
        settings: RagSettings,
    ) -> None:
        self._sessions = sessions
        self._provider = provider
        self._settings = settings

    async def search(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 8,
        kinds: Sequence[str] = (),
        source_ids: Sequence[str] = (),
        fresh_after: datetime | None = None,
        rerank: bool = True,
    ) -> RagAnswer:
        async with self._sessions() as session:
            outcome: HybridSearchResult = await hybrid_search(
                session,
                user_id=user_id,
                query=query,
                provider=self._provider,
                limit=limit,
                filters=HybridFilters(
                    kinds=tuple(kinds),
                    source_ids=tuple(source_ids),
                    fresh_after=fresh_after,
                ),
                rerank=rerank,
                vector_scan_limit=self._settings.vector_scan_limit,
                half_life_days=self._settings.freshness_half_life_days,
                stale_after_days=self._settings.stale_after_days,
            )
        return RagAnswer(
            query=query,
            results=tuple(_to_rag_hit(hit) for hit in outcome.hits),
            mode=outcome.mode,
            degraded=outcome.degraded,
            usage=outcome.usage,
            vector_backend=outcome.vector_backend,
        )


def _to_rag_hit(hit: HybridHit) -> RagHit:
    snippet = hit.text[:SNIPPET_CHARS]
    if len(hit.text) > SNIPPET_CHARS:
        snippet = f"{snippet}…"
    return RagHit(
        title=hit.title,
        url=hit.url,
        snippet=snippet,
        score=hit.score,
        kind=hit.kind,
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        source_id=hit.source_id,
        artifact_id=hit.artifact_id,
        captured_at=hit.captured_at,
        age_days=hit.age_days,
        stale=hit.stale,
        reasons=hit.reasons,
    )
