"""The additive async sync: `POST /sources/{id}/sync-jobs` queues a SYNC job.

The inline `POST /sources/{id}/sync` keeps its 200 report; this endpoint is the job+SSE variant.
A source linked to a v3 connection uses the normal per-connection SYNC job; a source without a
connection gets a payload-only job that the v4 dispatch replays by `source_id`. The real queue and
worker path is proven in `tests/jobs/test_sync_jobs_worker.py`; here the API contract, ownership,
refusals and the dedupe into an existing in-flight job.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.core.queue import InMemoryJobQueue
from botai.db.models import Job
from botai.sources.v4 import playbooks, service
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import FrozenClock, Sm, make_connection, signup
from tests.sources.v4.support import create_account

pytestmark = pytest.mark.db
BASE = "https://fakeuni.test"
SPEC: dict = {
    "version": 1,
    "reads": [
        {
            "name": "timetable",
            "kind": "schedule",
            "url": f"{BASE}/api/v2/timetable",
            "format": "json",
            "items_path": "/items",
            "fields": {"externalId": "/uid", "title": "/subject/name", "startsAt": "/slot/begin"},
        }
    ],
    "timeout_s": 10,
}


async def _space(client: httpx.AsyncClient, title: str = "Fakeuni") -> str:
    response = await client.post("/api/v1/spaces", json={"title": title})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _source(client: httpx.AsyncClient, space_id: str) -> dict:
    response = await client.post(
        "/api/v1/sources",
        json={"spaceId": space_id, "title": "ЛК Fakeuni", "url": f"{BASE}/app"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _attach_playbook(sm: Sm, user_id: str, source_id: str) -> str:
    async with sm() as session:
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
            source_id=source_id,
            patch=SourcePatch(playbook_id=playbook.id),
        )
        await session.commit()
        return source.id


async def _async_sync(client: httpx.AsyncClient, source_id: str) -> httpx.Response:
    return await client.post(f"/api/v1/sources/{source_id}/sync-jobs")


async def test_a_source_without_a_connection_queues_a_payload_only_job(
    v4_client: httpx.AsyncClient,
    queue: InMemoryJobQueue,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    source = await _source(v4_client, space_id)
    await _attach_playbook(tx_sessionmaker, account.id, source["id"])

    response = await _async_sync(v4_client, source["id"])
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"jobId", "sourceId", "status"}
    assert body["sourceId"] == source["id"]
    assert body["status"] == "queued"
    (job,) = queue.jobs
    assert job["id"] == body["jobId"]
    assert job["kind"] == JobKind.SYNC
    assert job["connection_id"] is None  # the dispatch path is by source_id
    assert job["payload"] == {"source_id": source["id"]}
    assert job["user_id"] == account.id


async def test_a_connected_source_uses_the_existing_per_connection_sync_job(
    v4_client: httpx.AsyncClient,
    queue: InMemoryJobQueue,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    connection_id = await make_connection(tx_sessionmaker, account.id, space_id)
    source = (
        await v4_client.post(
            "/api/v1/sources",
            json={
                "spaceId": space_id,
                "title": "ЛК Fakeuni",
                "url": f"{BASE}/app",
                "config": {"connection_id": connection_id},
            },
        )
    ).json()
    await _attach_playbook(tx_sessionmaker, account.id, source["id"])

    response = await _async_sync(v4_client, source["id"])
    assert response.status_code == 202, response.text
    (job,) = queue.jobs
    assert (job["kind"], job["connection_id"]) == (JobKind.SYNC, connection_id)
    assert job["payload"] == {"source_id": source["id"]}

    # the queue dedupes a task of one connection: the second request returns the same job
    again = await _async_sync(v4_client, source["id"])
    assert again.status_code == 202
    assert again.json()["jobId"] == response.json()["jobId"]
    assert len(queue.jobs) == 1


async def test_a_sync_already_in_flight_is_returned_instead_of_a_second_one(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    source = await _source(v4_client, space_id)
    await _attach_playbook(tx_sessionmaker, account.id, source["id"])
    running_id = new_id()
    async with tx_sessionmaker() as session:
        session.add(
            Job(
                id=running_id,
                user_id=account.id,
                kind=JobKind.SYNC.value,
                state="running",
                payload={"source_id": source["id"]},
                run_after=clock.now(),
            )
        )
        await session.commit()

    response = await _async_sync(v4_client, source["id"])
    assert response.status_code == 202
    assert response.json()["jobId"] == running_id
    assert response.json()["status"] == "running"
    async with tx_sessionmaker() as session:
        assert len(list(await session.scalars(select(Job)))) == 1


async def test_a_disabled_source_or_one_without_a_playbook_is_refused(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    source = await _source(v4_client, space_id)

    without_playbook = await _async_sync(v4_client, source["id"])
    assert without_playbook.status_code == 409
    assert "playbook" in without_playbook.json()["detail"]

    await _attach_playbook(tx_sessionmaker, account.id, source["id"])
    assert (await v4_client.post(f"/api/v1/sources/{source['id']}/disable")).status_code == 200
    disabled = await _async_sync(v4_client, source["id"])
    assert disabled.status_code == 409
    assert "выключен" in disabled.json()["detail"]


async def test_a_foreign_source_is_404(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    await signup(v4_client, tx_sessionmaker, clock)
    other = await create_account(tx_sessionmaker)
    async with tx_sessionmaker() as session:
        foreign = await service.create_source(
            session, user_id=other.user_id, space_id=other.space_id, title="Чужой"
        )
        await session.commit()
    assert (await _async_sync(v4_client, foreign.id)).status_code == 404


async def test_the_sync_jobs_endpoint_is_off_on_the_v3_engine(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    other = await create_account(tx_sessionmaker)
    async with tx_sessionmaker() as session:
        source = await service.create_source(
            session, user_id=other.user_id, space_id=other.space_id, title="Чужой"
        )
        await session.commit()
    response = await client.post(f"/api/v1/sources/{source.id}/sync-jobs")
    assert response.status_code == 404
    assert "v4" in response.json()["detail"]
