"""Seed helpers for RAG tests: owned rows that are removed by the `rag` fixture."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.ids import new_id
from botai.db.models import Chunk, ChunkEmbedding, Document, Source, SourceArtifact, Space, User

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(slots=True)
class RagSeed:
    """Creates owned rows and remembers users to delete after the test."""

    sessions: SessionFactory
    users: list[str] = field(default_factory=list)

    async def user(self, name: str = "Тест") -> str:
        user_id = new_id()
        async with self.sessions() as session:
            session.add(
                User(
                    id=user_id,
                    email=f"{user_id.lower()}@example.test",
                    display_name=name,
                    password_hash="x",
                )
            )
            await session.commit()
        self.users.append(user_id)
        return user_id

    async def space(self, user_id: str, title: str = "Тест-вуз") -> str:
        space_id = new_id()
        async with self.sessions() as session:
            session.add(Space(id=space_id, user_id=user_id, title=title))
            await session.commit()
        return space_id

    async def source(
        self, *, user_id: str, space_id: str, title: str = "Источник", trust: str = "normal"
    ) -> str:
        source_id = new_id()
        async with self.sessions() as session:
            session.add(
                Source(
                    id=source_id,
                    user_id=user_id,
                    space_id=space_id,
                    title=title,
                    entry_url="https://example.test/",
                    identity_key=f"site:example.test:{source_id}",
                    kinds=["page"],
                    status="active",
                    trust=trust,
                )
            )
            await session.commit()
        return source_id

    async def artifact(
        self,
        *,
        user_id: str,
        source_id: str,
        kind: str = "page",
        text: str | None = None,
        storage_ref: str | None = None,
        content_type: str | None = "text/plain",
        captured_at: datetime | None = None,
        metadata: dict[str, object] | None = None,
    ) -> str:
        artifact_id = new_id()
        payload = (text or "").encode("utf-8")
        extra = dict(metadata or {})
        if text is not None:
            extra["text"] = text
        async with self.sessions() as session:
            session.add(
                SourceArtifact(
                    id=artifact_id,
                    user_id=user_id,
                    source_id=source_id,
                    kind=kind,
                    url=f"https://example.test/{artifact_id}",
                    sha256=hashlib.sha256(payload).hexdigest(),
                    content_type=content_type,
                    size=len(payload),
                    storage_ref=storage_ref,
                    metadata_json=extra,
                    captured_at=captured_at or datetime.now(UTC),
                )
            )
            await session.commit()
        return artifact_id

    async def document_with_chunks(
        self,
        *,
        user_id: str,
        space_id: str,
        title: str,
        chunks: list[str],
        url: str | None = None,
        fetched_at: datetime | None = None,
        connection_id: str | None = None,
    ) -> str:
        document_id = new_id()
        moment = fetched_at or datetime.now(UTC)
        async with self.sessions() as session:
            session.add(
                Document(
                    id=document_id,
                    user_id=user_id,
                    space_id=space_id,
                    connection_id=connection_id,
                    kind="page",
                    title=title,
                    url=url or f"https://example.test/{document_id}",
                    mime="text/plain",
                    fetched_at=moment,
                    hash=hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest(),
                )
            )
            await session.flush()
            for order, text in enumerate(chunks):
                session.add(
                    Chunk(
                        user_id=user_id,
                        document_id=document_id,
                        ord=order,
                        text=text,
                        tokens=len(text) // 3,
                    )
                )
            await session.commit()
        return document_id

    async def chunk_embedding(
        self,
        *,
        user_id: str,
        chunk_id: str,
        document_id: str,
        model: str,
        vector: list[float],
        source_id: str | None = None,
        artifact_id: str | None = None,
        captured_at: datetime | None = None,
        kind: str | None = None,
        trust: str | None = None,
    ) -> str:
        embedding_id = new_id()
        async with self.sessions() as session:
            session.add(
                ChunkEmbedding(
                    id=embedding_id,
                    user_id=user_id,
                    chunk_id=chunk_id,
                    document_id=document_id,
                    source_id=source_id,
                    artifact_id=artifact_id,
                    model=model,
                    dimension=len(vector),
                    content_hash=hashlib.sha256(b"seed").hexdigest(),
                    embedding=vector,
                    kind=kind,
                    captured_at=captured_at,
                    trust=trust,
                )
            )
            await session.commit()
        return embedding_id
