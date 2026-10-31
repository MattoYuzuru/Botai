"""Artifacts of a source: what a run read or saved, content-addressed and scoped to its owner.

Bytes are not stored here: `storage_ref` points at the artifact store outside the database,
`sha256` is the content address. A run id belongs to the same user, so one student cannot attach an
artifact to another's run.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Job, SourceArtifact, SourceState
from botai.sources.v4 import kinds as kinds_vocab
from botai.sources.v4 import store

MAX_LIMIT = 500
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _validation(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, detail)


def _digest(raw: str) -> str:
    value = raw.strip().casefold()
    if not _SHA256.match(value):
        raise _validation("Отпечаток артефакта должен быть sha256 в шестнадцатеричном виде")
    return value


async def add_artifact(
    session: AsyncSession,
    *,
    user_id: str,
    source_id: str,
    kind: str,
    sha256: str,
    url: str | None = None,
    content_type: str | None = None,
    size: int | None = None,
    storage_ref: str | None = None,
    metadata: Mapping[str, object] | None = None,
    captured_at: datetime | None = None,
    run_id: str | None = None,
    step: int | None = None,
) -> SourceArtifact:
    """Record one artifact of a source (the runner calls this after reading something)."""
    await store.owned_source(session, user_id, source_id)
    if run_id is not None:
        job = await session.scalar(select(Job).where(Job.id == run_id, Job.user_id == user_id))
        if job is None:
            raise store.not_found("Задание не найдено")
    if size is not None and size < 0:
        raise _validation("Размер артефакта не может быть отрицательным")
    if step is not None and step < 0:
        raise _validation("Номер шага не может быть отрицательным")
    row = SourceArtifact(
        user_id=user_id,
        source_id=source_id,
        kind=kinds_vocab.validate_artifact_kind(kind),
        url=url,
        sha256=_digest(sha256),
        content_type=content_type,
        size=size,
        storage_ref=storage_ref,
        metadata_json=dict(metadata) if metadata is not None else {},
        captured_at=captured_at or datetime.now(UTC),
        run_id=run_id,
        step=step,
    )
    session.add(row)
    await session.flush()
    return row


async def list_artifacts(
    session: AsyncSession,
    *,
    user_id: str,
    source_id: str,
    kind: str | None = None,
    limit: int = 100,
) -> list[SourceArtifact]:
    """Newest artifacts of one source, optionally of one kind."""
    await store.owned_source(session, user_id, source_id)
    if limit < 1 or limit > MAX_LIMIT:
        raise _validation(f"limit должен быть от 1 до {MAX_LIMIT}")
    statement = select(SourceArtifact).where(
        SourceArtifact.user_id == user_id, SourceArtifact.source_id == source_id
    )
    if kind is not None:
        statement = statement.where(SourceArtifact.kind == kinds_vocab.validate_artifact_kind(kind))
    statement = statement.order_by(SourceArtifact.captured_at.desc(), SourceArtifact.id.desc())
    return list(await session.scalars(statement.limit(limit)))


# --- content-addressed storage of artifact bytes --------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactBlob:
    """Where the bytes of an artifact went and how to recognise them."""

    sha256: str
    size: int
    storage_ref: str


class ArtifactStore(Protocol):
    """Stores artifact bytes outside the database and returns their content address."""

    def put(self, data: bytes) -> ArtifactBlob: ...

    def get(self, storage_ref: str) -> bytes: ...


class FileArtifactStore:
    """The simplest artifact store: one file per content address under a configured directory."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def put(self, data: bytes) -> ArtifactBlob:
        digest = hashlib.sha256(data).hexdigest()
        target = self._path(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
        return ArtifactBlob(sha256=digest, size=len(data), storage_ref=str(target))

    def get(self, storage_ref: str) -> bytes:
        """Blob bytes; `storage_ref` may be an absolute path, a path under the root, or a digest."""
        value = storage_ref.strip()
        digest = value.casefold()
        if _SHA256.match(digest):
            return self._path(digest).read_bytes()
        path = Path(value)
        if not path.is_absolute():
            path = self._root / path
        return path.read_bytes()

    def reference(self, digest: str) -> str:
        """The `storage_ref` of a content address in this store (a path that does not exist yet)."""
        return str(self._path(digest.casefold()))

    def refs(self) -> list[str]:
        """Every blob of the store; `.tmp` leftovers and foreign files are not blobs."""
        out: list[str] = []
        for path in sorted(self._root.glob("*/*")):
            if path.is_file() and path.suffix != ".tmp" and _SHA256.match(path.name):
                out.append(str(path))
        return out

    def delete(self, storage_ref: str) -> bool:
        """Remove one blob; the shard directory goes too when it became empty."""
        path = Path(storage_ref)
        if not path.is_absolute():
            path = self._root / path
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        with contextlib.suppress(OSError):
            path.parent.rmdir()
        return True

    def _path(self, digest: str) -> Path:
        return self._root / digest[:2] / digest


# --- retention ------------------------------------------------------------------------------------

_SHA256_TEXT = re.compile(r"\b[a-f0-9]{64}\b")


def _blob_age_s(storage_ref: str, moment: datetime) -> float:
    """Seconds since the blob file was written (sync helper: the async cleaner must not block)."""
    return moment.timestamp() - Path(storage_ref).stat().st_mtime


async def referenced_storage_refs(session: AsyncSession, *, user_id: str | None = None) -> set[str]:
    """Storage refs and content addresses the database still cites (never to be deleted).

    Sources of citation: every `source_artifacts` row (`storage_ref` and `sha256`), and every
    64-hex digest mentioned in `source_state` (a cursor, site map or endpoint list may point at the
    artifact a later run will reuse). RAG chunks do not reference source artifacts yet; when they
    do, their column belongs in this set too (D-011 retention must respect citations).
    """
    refs: set[str] = set()
    artifact_stmt = select(SourceArtifact.storage_ref, SourceArtifact.sha256)
    if user_id is not None:
        artifact_stmt = artifact_stmt.where(SourceArtifact.user_id == user_id)
    for storage_ref, sha256 in (await session.execute(artifact_stmt)).all():
        if storage_ref:
            refs.add(str(storage_ref))
        if sha256:
            refs.add(str(sha256).casefold())
    state_stmt = select(SourceState.cursor, SourceState.site_map, SourceState.endpoints)
    for cursor, site_map, endpoints in (await session.execute(state_stmt)).all():
        for document in (cursor, site_map, endpoints):
            refs.update(_SHA256_TEXT.findall(json.dumps(document, ensure_ascii=False)))
    return refs


async def cleanup_unreferenced(
    session: AsyncSession,
    store: FileArtifactStore,
    *,
    user_id: str | None = None,
    keep: Iterable[str] = (),
    min_age_s: float = 0.0,
    now: datetime | None = None,
) -> int:
    """Delete blobs of `store` that no row cites; returns how many files were removed.

    `min_age_s` protects a blob a run has just written but not yet cited: retention never races a
    live run. A blob is kept when its path or its content address is cited (a citation by sha256
    survives a moved store root).
    """
    referenced = await referenced_storage_refs(session, user_id=user_id)
    referenced.update(str(item) for item in keep)
    cited_digests = {
        value.casefold() for value in referenced if _SHA256.fullmatch(value.casefold())
    }
    moment = now or datetime.now(UTC)
    removed = 0
    for storage_ref in store.refs():
        digest = Path(storage_ref).name
        if storage_ref in referenced or digest in cited_digests:
            continue
        if min_age_s > 0 and _blob_age_s(storage_ref, moment) < min_age_s:
            continue
        if store.delete(storage_ref):
            removed += 1
    return removed
