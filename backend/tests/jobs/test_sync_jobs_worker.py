"""The async sync on the real queue/worker: `sync-jobs` → `SYNC` job → `SourceRunner.sync`.

No model and no network: fakeuni is the synthetic university. A source that has no v3 connection
is synced by its `source_id` (the dispatch branch `_sync_source_by_id`), the runner records status,
freshness and artifacts, and the job result carries the counts. A queued source is not queued twice.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from botai.agent.v4.dispatch import make_v4_handlers
from botai.core.jobs import JobKind
from botai.db.models import Job, ScheduleEvent, Source, SourceArtifact, User
from botai.sources.v4 import playbooks, service
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import make_space
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env

pytestmark = pytest.mark.db
BASE = "https://fakeuni.test"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
SPEC: dict[str, Any] = {
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
        {
            "name": "deadlines",
            "kind": "deadlines",
            "url": f"{BASE}/api/v2/tasks/deadlines",
            "format": "json",
            "items_path": "",
            "fields": {
                "externalId": "/taskId",
                "title": "/name",
                "dueAt": {"pointer": "/due", "transform": "unixSeconds"},
                "state": "/status",
            },
        },
    ],
    "timeout_s": 10,
}


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


async def _make_source(env: Env, user_id: str, *, with_playbook: bool = True) -> tuple[str, str]:
    """A source of the user's university with no v3 connection; returns `(space_id, source_id)`."""
    space_id = await make_space(env.sm, user_id)
    async with env.sm() as session:
        source = await service.create_source(
            session,
            user_id=user_id,
            space_id=space_id,
            title="Личный кабинет",
            entry_url=f"{BASE}/app",
            kinds=["site", "schedule", "deadlines"],
            config={},  # no connection_id: the REST/agent-created source
        )
        if with_playbook:
            playbook = await playbooks.create_playbook(
                session, user_id=user_id, title="Fakeuni", university_id=None
            )
            version = await playbooks.add_playbook_version(
                session, user_id=user_id, playbook_id=playbook.id, spec=SPEC
            )
            await playbooks.record_canary(
                session,
                user_id=user_id,
                playbook_id=playbook.id,
                version=version.version,
                canary={"ok": True},
            )
            await playbooks.activate_playbook_version(
                session, user_id=user_id, playbook_id=playbook.id, version=version.version
            )
            source = await service.update_source(
                session,
                user_id=user_id,
                source_id=source.id,
                patch=SourcePatch(playbook_id=playbook.id),
            )
        await session.commit()
        return space_id, source.id


async def _sync_jobs(env: Env, client: httpx.AsyncClient, source_id: str) -> httpx.Response:
    return await client.post(f"/api/v1/sources/{source_id}/sync-jobs")


async def test_sync_jobs_commits_a_real_sync_job_and_dedupes_it(
    env: Env, tmp_path: Path, fakeuni_transport: httpx.ASGITransport
) -> None:
    env.settings.agent_engine = "v4"
    client = env.new_client()
    user = await env.account(client)
    _, source_id = await _make_source(env, user.id)

    first = await _sync_jobs(env, client, source_id)
    assert first.status_code == 202, first.text
    body = first.json()
    assert set(body) == {"jobId", "sourceId", "status"}
    assert body["sourceId"] == source_id
    assert body["status"] == "queued"
    async with env.sm() as session:
        job = await session.get(Job, body["jobId"])
        assert job is not None
        assert job.user_id == user.id
        assert (job.kind, job.state, job.connection_id) == (JobKind.SYNC.value, "queued", None)
        assert job.payload == {"source_id": source_id}

    # the same source is not queued twice
    again = await _sync_jobs(env, client, source_id)
    assert again.status_code == 202
    assert again.json()["jobId"] == body["jobId"]
    async with env.sm() as session:
        count = await session.scalar(
            select(func.count()).select_from(Job).where(Job.user_id == user.id)
        )
    assert count == 1


async def test_the_worker_syncs_a_source_without_a_connection_by_source_id(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
) -> None:
    env.settings.agent_engine = "v4"
    client = env.new_client()
    user = await env.account(client)
    _, source_id = await _make_source(env, user.id)

    response = await _sync_jobs(env, client, source_id)
    assert response.status_code == 202, response.text
    job_id = response.json()["jobId"]
    env.start(
        env.worker(
            make_v4_handlers(
                http_transport=fakeuni_transport,
                artifact_root=tmp_path / "artifacts",
                source_cookies=fakeuni_cookies,
            )
        )
    )
    job = await env.wait_for(job_id, {"done"}, within=60)

    assert job.result is not None
    assert job.result["status"] == "ok", job.result
    assert job.result["source_id"] == source_id
    assert job.result["counts"]["schedule"] == 4
    assert job.result["counts"]["deadlines"] == 2

    async with env.sm() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        assert source.status == "active"
        assert source.last_sync_at is not None
        assert source.last_error is None
        artifacts = list(
            await session.scalars(
                select(SourceArtifact).where(SourceArtifact.source_id == source_id)
            )
        )
        assert artifacts
        # the sync wrote the product domain with `source_id` provenance
        events = await session.scalar(
            select(func.count())
            .select_from(ScheduleEvent)
            .where(ScheduleEvent.source_id == source_id)
        )
        assert events == 4
    async with env.sm() as session:
        user_row = await session.get(User, user.id)
        assert user_row is not None


async def test_a_sync_without_a_playbook_fails_the_job_honestly(
    env: Env, tmp_path: Path, fakeuni_transport: httpx.ASGITransport
) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    _, source_id = await _make_source(env, user.id, with_playbook=False)

    job_id = await env.services.queue.enqueue(
        JobKind.SYNC, user_id=user.id, payload={"source_id": source_id}
    )
    env.start(
        env.worker(
            make_v4_handlers(
                http_transport=fakeuni_transport,
                artifact_root=tmp_path / "artifacts",
            )
        )
    )
    job = await env.wait_for(job_id, {"failed"}, within=60)
    assert job.error is not None
    assert job.error["code"] == "conflict"
    assert "playbook" in str(job.error.get("detail", ""))
    async with env.sm() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        assert source.status == "draft"  # nothing was claimed
