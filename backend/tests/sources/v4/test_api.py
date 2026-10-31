"""REST and SSE of v4 sources: flag, CRUD, scope, playbook flow, artifacts, state, priorities."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.auth.support import FrozenClock, Sm, signup

pytestmark = pytest.mark.db
BASE = "https://fakeuni.test"
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
            },
        },
    ],
    "timeout_s": 10,
}


async def make_space(client: httpx.AsyncClient, title: str = "Fakeuni") -> str:
    response = await client.post("/api/v1/spaces", json={"title": title})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def make_source(
    client: httpx.AsyncClient,
    space_id: str,
    *,
    title: str = "ЛК Fakeuni",
    url: str = f"{BASE}/app",
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/sources",
        json={
            "spaceId": space_id,
            "title": title,
            "url": url,
            "kinds": kinds or ["site", "schedule", "deadlines"],
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_the_v4_api_is_disabled_on_the_v3_engine(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    response = await client.get("/api/v1/sources")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert "BOTAI_AGENT_ENGINE=v4" in body["detail"]


async def test_create_read_patch_and_scope(
    v4_client: httpx.AsyncClient, v4_app: FastAPI, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(v4_client, tx_sessionmaker, clock)
    space = await make_space(v4_client)
    source = await make_source(v4_client, space)
    assert source["title"] == "ЛК Fakeuni"
    assert source["spaceId"] == space
    assert source["status"] == "draft"
    assert source["kinds"] == ["site", "schedule", "deadlines"]
    assert "identityKey" not in source  # the identity digest is not part of the API surface

    listed = (await v4_client.get("/api/v1/sources")).json()
    assert [row["id"] for row in listed] == [source["id"]]

    patched = (
        await v4_client.patch(
            f"/api/v1/sources/{source['id']}",
            json={"title": "Новое имя", "trust": "high", "kinds": ["lms", "schedule"]},
        )
    ).json()
    assert patched["title"] == "Новое имя"
    assert patched["trust"] == "high"
    assert patched["kinds"] == ["lms", "schedule"]

    duplicate = await v4_client.post(
        "/api/v1/sources",
        json={"spaceId": space, "title": "Дубль", "url": f"{BASE}/app"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "conflict"

    disabled = (await v4_client.post(f"/api/v1/sources/{source['id']}/disable")).json()
    assert disabled["status"] == "disabled"
    enabled = (await v4_client.post(f"/api/v1/sources/{source['id']}/enable")).json()
    assert enabled["status"] == "draft"

    # Another student sees nothing of this source.
    other = v4_client.__class__(transport=v4_client._transport, base_url=str(v4_client.base_url))
    try:
        await signup(other, tx_sessionmaker, clock)
        assert (await other.get("/api/v1/sources")).json() == []
        assert (await other.get(f"/api/v1/sources/{source['id']}")).status_code == 404
    finally:
        await other.aclose()


async def test_playbook_versions_canary_activation_sync_and_artifacts(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    fakeuni_cookies: dict[str, str],
) -> None:
    await signup(v4_client, tx_sessionmaker, clock)
    space = await make_space(v4_client)
    source = await make_source(v4_client, space)

    playbook = (
        await v4_client.post(f"/api/v1/sources/{source['id']}/playbook", json={"title": "Fakeuni"})
    ).json()
    assert playbook["currentVersion"] == 0

    version = (
        await v4_client.post(
            f"/api/v1/sources/{source['id']}/playbook/versions",
            json={"spec": SCHEDULE_SPEC},
        )
    ).json()
    assert version["version"] == 1
    assert version["active"] is False

    early = await v4_client.post(f"/api/v1/sources/{source['id']}/playbook/versions/1/activate")
    assert early.status_code == 409
    assert early.json()["code"] == "conflict"

    canary = await v4_client.post(f"/api/v1/sources/{source['id']}/canary", json={})
    assert canary.status_code == 200, canary.text
    assert canary.json()["ok"] is True
    assert canary.json()["counts"] == {"schedule": 4, "deadlines": 2}

    activated = (
        await v4_client.post(f"/api/v1/sources/{source['id']}/playbook/versions/1/activate")
    ).json()
    assert activated["active"] is True

    synced = await v4_client.post(f"/api/v1/sources/{source['id']}/sync", json={})
    assert synced.status_code == 200, synced.text
    report = synced.json()
    assert report["ok"] is True
    assert report["sourceStatus"] == "active"
    assert report["counts"] == {"schedule": 4, "deadlines": 2}

    fresh = (await v4_client.get(f"/api/v1/sources/{source['id']}")).json()
    assert fresh["status"] == "active"
    assert fresh["lastSyncAt"] is not None
    state = (await v4_client.get(f"/api/v1/sources/{source['id']}/state")).json()
    assert state["lastRevision"] == 1
    assert [endpoint["name"] for endpoint in state["endpoints"]] == ["timetable", "deadlines"]
    artifacts = (await v4_client.get(f"/api/v1/sources/{source['id']}/artifacts")).json()
    assert len(artifacts) == 2
    assert {row["kind"] for row in artifacts} == {"json"}
    database = await v4_client.get("/api/v1/sources")
    token = next(iter(fakeuni_cookies.values()))
    assert token not in json.dumps(database.json(), ensure_ascii=False)
    assert token not in json.dumps(artifacts, ensure_ascii=False)
    assert token not in json.dumps(state, ensure_ascii=False)


async def test_sync_stream_reports_progress_and_done(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    fakeuni_cookies: dict[str, str],
) -> None:
    await signup(v4_client, tx_sessionmaker, clock)
    space = await make_space(v4_client)
    source = await make_source(v4_client, space)
    await v4_client.post(f"/api/v1/sources/{source['id']}/playbook", json={"title": "Fakeuni"})
    await v4_client.post(
        f"/api/v1/sources/{source['id']}/playbook/versions", json={"spec": SCHEDULE_SPEC}
    )
    await v4_client.post(f"/api/v1/sources/{source['id']}/canary", json={})
    await v4_client.post(f"/api/v1/sources/{source['id']}/playbook/versions/1/activate")
    response = await v4_client.get(f"/api/v1/sources/{source['id']}/sync/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "event: read" in body
    assert "event: done" in body
    assert '"ok": true' in body


async def test_priorities_round_trip(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(v4_client, tx_sessionmaker, clock)
    space = await make_space(v4_client)
    first = await make_source(v4_client, space, title="Расписание", url=f"{BASE}/app")
    second = await make_source(v4_client, space, title="Календарь", url=f"{BASE}/calendar")
    put = await v4_client.put(
        f"/api/v1/spaces/{space}/priorities/schedule",
        json={"sourceIds": [second["id"], first["id"], second["id"]]},
    )
    assert put.status_code == 200, put.text
    assert put.json()["sourceIds"] == [second["id"], first["id"]]
    got = (await v4_client.get(f"/api/v1/spaces/{space}/priorities/schedule")).json()
    assert got["sourceIds"] == [second["id"], first["id"]]
