"""RAG auto-index after a successful v4 source sync (D-011 remaining item, task-15).

A fakeuni SYNC saves JSON artifacts; the dispatch then asks `search.rag` to chunk and embed them.
The hook is soft and shared by both SYNC paths (connection-linked and `source_id`-only): a repeated
sync adds no rows, a disabled flag or a broken indexer leaves the job `done` with an honest warning,
and the job result contract does not change.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from botai.agent.v4.dispatch import make_v4_handlers
from botai.core.jobs import JobKind
from botai.db.models import Chunk, ChunkEmbedding, Document, Job, SourceArtifact
from botai.search.rag import RagSettings
from botai.settings import Settings
from botai.sources.v4 import playbooks, service
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import make_space
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env

pytestmark = pytest.mark.db

BASE = "https://fakeuni.test"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
# Default local provider width; the migrated test column is vector(384) too.
DIMENSION = 384
SCHEDULE_SPEC: dict[str, Any] = {
    "version": 1,
    "reads": [
        {
            "name": "timetable",
            "kind": "schedule",
            "url": f"{BASE}/api/v2/timetable",
            "format": "json",
            "items_path": "/items",
            "fields": {
                "externalId": "/uid",
                "title": "/subject/name",
                "startsAt": "/slot/begin",
                "endsAt": "/slot/end",
                "location": "/place/room",
                "kind": "/type",
            },
        },
    ],
    "timeout_s": 10,
}
ARTIFACTS = "artifacts"


@pytest.fixture(autouse=True)
def _clean_fakeuni() -> Any:
    fakeuni_api.reset()
    yield
    fakeuni_api.reset()


@pytest.fixture
def fakeuni_cookies() -> dict[str, str]:
    token = secrets.token_hex(16)
    fakeuni_login.SESSIONS.add(token)
    return {fakeuni_login.COOKIE: token}


@pytest.fixture
def fakeuni_transport(fakeuni_app: Any) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=fakeuni_app)


def _enable_rag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production flag is off in tests; the hook itself must be proven with RAG on."""
    monkeypatch.setattr(
        "botai.search.rag.rag_settings", lambda: RagSettings(enabled=True, dimension=DIMENSION)
    )


async def _make_source_with_playbook(
    env: Env,
    *,
    user_id: str,
    space_id: str,
    connection_id: str | None,
) -> str:
    async with env.sm() as session:
        source = await service.create_source(
            session,
            user_id=user_id,
            space_id=space_id,
            title="Личный кабинет",
            entry_url=f"{BASE}/app",
            kinds=["schedule"],
            config={"connection_id": connection_id} if connection_id else {},
        )
        playbook = await playbooks.create_playbook(
            session, user_id=user_id, title="Fakeuni", university_id=None
        )
        version = await playbooks.add_playbook_version(
            session,
            user_id=user_id,
            playbook_id=playbook.id,
            spec=SCHEDULE_SPEC,
            canary={"ok": True, "checked_at": NOW.isoformat()},
        )
        await playbooks.activate_playbook_version(
            session, user_id=user_id, playbook_id=playbook.id, version=version.version
        )
        updated = await service.update_source(
            session,
            user_id=user_id,
            source_id=source.id,
            patch=SourcePatch(playbook_id=playbook.id),
        )
        await session.commit()
        return updated.id


def _worker(
    env: Env, tmp_path: Path, transport: httpx.ASGITransport, cookies: dict[str, str]
) -> None:
    env.start(
        env.worker(
            make_v4_handlers(
                http_transport=transport,
                artifact_root=tmp_path / ARTIFACTS,
                source_cookies=cookies,
            )
        )
    )


async def _sync(
    env: Env,
    *,
    user_id: str,
    connection_id: str | None = None,
    source_id: str | None = None,
) -> Job:
    job_id = await env.services.queue.enqueue(
        JobKind.SYNC,
        user_id=user_id,
        connection_id=connection_id,
        payload={"source_id": source_id} if source_id else {},
    )
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    return job


async def _counts(env: Env, user_id: str) -> tuple[int, int, int]:
    async with env.sm() as session:
        documents = int(
            await session.scalar(
                select(func.count()).select_from(Document).where(Document.user_id == user_id)
            )
            or 0
        )
        chunks = int(
            await session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.user_id == user_id)
            )
            or 0
        )
        embeddings = int(
            await session.scalar(
                select(func.count())
                .select_from(ChunkEmbedding)
                .where(ChunkEmbedding.user_id == user_id)
            )
            or 0
        )
    return documents, chunks, embeddings


async def test_sync_indexes_artifacts_for_rag(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rag(monkeypatch)
    env.settings.agent_engine = "v4"
    user = await env.account()
    space_id, connection_id = await env.connection(user.id, status="draft", origin=BASE, kind="lms")
    source_id = await _make_source_with_playbook(
        env, user_id=user.id, space_id=space_id, connection_id=connection_id
    )
    _worker(env, tmp_path, fakeuni_transport, fakeuni_cookies)

    job = await _sync(env, user_id=user.id, connection_id=connection_id)

    assert job.result is not None
    assert job.result["status"] == "ok", job.result
    assert "rag" not in job.result
    assert "autoindex" not in job.result, "the hook must not change the RunReportOut-shaped body"

    async with env.sm() as session:
        artifacts = list(
            await session.scalars(
                select(SourceArtifact).where(SourceArtifact.source_id == source_id)
            )
        )
        chunks = list(await session.scalars(select(Chunk).where(Chunk.user_id == user.id)))
        embeddings = list(
            await session.scalars(select(ChunkEmbedding).where(ChunkEmbedding.user_id == user.id))
        )
    assert artifacts, "the sync must have saved the JSON read as an artifact"
    assert chunks, "the fresh artifacts must be chunked for RAG"
    assert len(embeddings) == len(chunks)
    assert {row.source_id for row in embeddings} == {source_id}
    assert {row.artifact_id for row in embeddings} == {artifact.id for artifact in artifacts}
    assert all(row.artifact_id is not None for row in embeddings)
    assert {row.model for row in embeddings} == {"local-hash-v1"}
    # The indexer writes the derived pgvector column too when the width matches the migration.
    assert all(row.embedding_vec is not None for row in embeddings)


async def test_repeated_sync_does_not_duplicate_the_index(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rag(monkeypatch)
    env.settings.agent_engine = "v4"
    user = await env.account()
    space_id, connection_id = await env.connection(user.id, status="draft", origin=BASE, kind="lms")
    await _make_source_with_playbook(
        env, user_id=user.id, space_id=space_id, connection_id=connection_id
    )
    _worker(env, tmp_path, fakeuni_transport, fakeuni_cookies)

    first = await _sync(env, user_id=user.id, connection_id=connection_id)
    counts_after_first = await _counts(env, user.id)
    second = await _sync(env, user_id=user.id, connection_id=connection_id)
    counts_after_second = await _counts(env, user.id)

    assert first.result is not None
    assert first.result["status"] == "ok"
    assert second.result is not None
    assert second.result["status"] == "ok"
    assert counts_after_first[0] > 0
    assert counts_after_first[2] > 0
    assert counts_after_second == counts_after_first, "a repeated sync must not duplicate the index"


async def test_autoindex_works_for_a_source_without_a_connection(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second SYNC path (`_sync_source_by_id`) must index as well: no connection row exists."""
    _enable_rag(monkeypatch)
    env.settings.agent_engine = "v4"
    user = await env.account()
    space_id = await make_space(env.sm, user.id)
    source_id = await _make_source_with_playbook(
        env, user_id=user.id, space_id=space_id, connection_id=None
    )
    _worker(env, tmp_path, fakeuni_transport, fakeuni_cookies)

    job = await _sync(env, user_id=user.id, source_id=source_id)

    assert job.result is not None
    assert job.result["status"] == "ok", job.result
    async with env.sm() as session:
        embeddings = list(
            await session.scalars(select(ChunkEmbedding).where(ChunkEmbedding.user_id == user.id))
        )
    assert embeddings
    assert {row.source_id for row in embeddings} == {source_id}


async def test_autoindex_switch_off_leaves_the_job_green(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rag(monkeypatch)
    env.settings.agent_engine = "v4"
    env.settings.rag_autoindex = False
    user = await env.account()
    space_id, connection_id = await env.connection(user.id, status="draft", origin=BASE, kind="lms")
    await _make_source_with_playbook(
        env, user_id=user.id, space_id=space_id, connection_id=connection_id
    )
    _worker(env, tmp_path, fakeuni_transport, fakeuni_cookies)

    job = await _sync(env, user_id=user.id, connection_id=connection_id)

    assert job.result is not None
    assert job.result["status"] == "ok"
    assert await _counts(env, user.id) == (0, 0, 0)


async def test_rag_disabled_leaves_the_job_green(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOTAI_RAG_ENABLED", raising=False)
    env.settings.agent_engine = "v4"
    user = await env.account()
    space_id, connection_id = await env.connection(user.id, status="draft", origin=BASE, kind="lms")
    await _make_source_with_playbook(
        env, user_id=user.id, space_id=space_id, connection_id=connection_id
    )
    _worker(env, tmp_path, fakeuni_transport, fakeuni_cookies)

    job = await _sync(env, user_id=user.id, connection_id=connection_id)

    assert job.result is not None
    assert job.result["status"] == "ok"
    assert await _counts(env, user.id) == (0, 0, 0)


async def test_indexer_failure_is_a_warning_and_not_a_failed_job(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _enable_rag(monkeypatch)
    env.settings.agent_engine = "v4"

    async def broken(*_: object, **__: object) -> object:
        raise RuntimeError("boom-with-sensitive-detail")

    monkeypatch.setattr("botai.search.rag.reindex_source_artifacts", broken)
    user = await env.account()
    space_id, connection_id = await env.connection(user.id, status="draft", origin=BASE, kind="lms")
    await _make_source_with_playbook(
        env, user_id=user.id, space_id=space_id, connection_id=connection_id
    )
    _worker(env, tmp_path, fakeuni_transport, fakeuni_cookies)

    with caplog.at_level(logging.WARNING, logger="botai.search.rag"):
        job = await _sync(env, user_id=user.id, connection_id=connection_id)

    assert job.result is not None
    assert job.result["status"] == "ok", "an indexer bug must not fail a sync"
    warnings = [record for record in caplog.records if record.name == "botai.search.rag"]
    assert warnings, "the failure must be visible in the log"
    message = " ".join(record.getMessage() for record in warnings)
    assert "RuntimeError" in message
    assert "boom-with-sensitive-detail" not in message, "log only the exception class, not its text"
    assert await _counts(env, user.id) == (0, 0, 0)


def test_autoindex_setting_defaults_on_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).rag_autoindex is True
    monkeypatch.setenv("BOTAI_RAG_AUTOINDEX", "false")
    assert Settings(_env_file=None).rag_autoindex is False
