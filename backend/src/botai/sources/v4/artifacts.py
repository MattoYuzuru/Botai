"""Artifacts of a source: what a run read or saved, content-addressed and scoped to its owner.

Bytes are not stored here: `storage_ref` points at the artifact store outside the database,
`sha256` is the content address. A run id belongs to the same user, so one student cannot attach an
artifact to another's run.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Job, SourceArtifact
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
        path = Path(storage_ref)
        if not path.is_absolute():
            path = self._root / path
        return path.read_bytes()

    def _path(self, digest: str) -> Path:
        return self._root / digest[:2] / digest
