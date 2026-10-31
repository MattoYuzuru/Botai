"""L1 proof that a v4 playbook sync feeds the product read screens.

A real `SourceRunner.sync` against the synthetic university (ASGI transport, no network) writes the
domain rows with `source_id`; the read APIs of the product then return them for the right space, and
the v4 priority setting decides the primary source. The v3-only answers are covered by
`tests/domain/test_read_path_v4.py`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from botai.sources.v4 import playbooks
from botai.sources.v4 import service as v4_service
from botai.sources.v4.schemas import SourcePatch
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


async def _space(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/v1/spaces", json={"title": "Fakeuni"})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _source(client: httpx.AsyncClient, space_id: str, *, title: str, url: str) -> str:
    response = await client.post(
        "/api/v1/sources",
        json={
            "spaceId": space_id,
            "title": title,
            "url": url,
            "kinds": ["site", "schedule", "deadlines"],
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _attach_playbook(
    sm: Sm, user_id: str, source_id: str, *, university_id: str | None = None
) -> None:
    async with sm() as session:
        playbook = await playbooks.create_playbook(
            session, user_id=user_id, title="Fakeuni", university_id=university_id
        )
        version = await playbooks.add_playbook_version(
            session, user_id=user_id, playbook_id=playbook.id, spec=SCHEDULE_SPEC
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
        await v4_service.update_source(
            session,
            user_id=user_id,
            source_id=source_id,
            patch=SourcePatch(playbook_id=playbook.id),
        )
        await session.commit()


async def _sync(client: httpx.AsyncClient, source_id: str) -> dict[str, Any]:
    response = await client.post(f"/api/v1/sources/{source_id}/sync", json={})
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _get(client: httpx.AsyncClient, path: str, **params: str) -> Any:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


WINDOW = {"start": "2026-10-26T00:00:00Z", "end": "2026-11-01T23:00:00Z"}


async def test_a_fakeuni_sync_feeds_the_read_apis_of_its_space(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    source_id = await _source(v4_client, space_id, title="ЛК Fakeuni", url=f"{BASE}/app")
    await _attach_playbook(tx_sessionmaker, account.id, source_id)

    report = await _sync(v4_client, source_id)
    assert report["ok"] is True, report["errors"]
    assert report["domain"]["schedule"]["inserted"] == 4
    assert report["domain"]["deadlines"]["inserted"] == 2

    schedule = await _get(v4_client, "/api/v1/schedule", **WINDOW)
    assert len(schedule) == 4
    assert {event["provenance"]["sourceId"] for event in schedule} == {source_id}
    assert {event["provenance"]["spaceId"] for event in schedule} == {space_id}
    assert all(event["provenance"]["connectionId"] is None for event in schedule)
    assert schedule[0]["sources"] == [
        {"connectionId": None, "sourceId": source_id, "title": "ЛК Fakeuni"}
    ]

    deadlines = await _get(v4_client, "/api/v1/deadlines")
    assert len(deadlines) == 2
    assert {deadline["provenance"]["sourceId"] for deadline in deadlines} == {source_id}

    today = await _get(v4_client, "/api/v1/today")
    assert today["events"] == []  # the fake timetable is next week, not today (2026-10-27)

    sections = await _get(v4_client, f"/api/v1/spaces/{space_id}/sections")
    assert {section["kind"]: section["count"] for section in sections} == {
        "scheduleEvents": 4,
        "deadlines": 2,
    }

    # a disabled source hides its rows from every read, and nothing is deleted
    assert (await v4_client.post(f"/api/v1/sources/{source_id}/disable")).status_code == 200
    assert await _get(v4_client, "/api/v1/deadlines") == []
    assert await _get(v4_client, "/api/v1/schedule", **WINDOW) == []
    assert await _get(v4_client, f"/api/v1/spaces/{space_id}/sections") == []
    assert (await v4_client.post(f"/api/v1/sources/{source_id}/enable")).status_code == 200
    assert len(await _get(v4_client, "/api/v1/deadlines")) == 2


async def test_two_synced_sources_of_one_university_merge_and_the_priority_decides(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    cabinet = await _source(v4_client, space_id, title="Кабинет", url=f"{BASE}/app")
    calendar = await _source(v4_client, space_id, title="Календарь", url=f"{BASE}/second")
    await _attach_playbook(tx_sessionmaker, account.id, cabinet)
    await _attach_playbook(tx_sessionmaker, account.id, calendar)
    await _sync(v4_client, cabinet)
    await _sync(v4_client, calendar)

    merged = await _get(v4_client, "/api/v1/schedule", **WINDOW)
    assert len(merged) == 4  # the same classes of the fake university, merged one-to-one
    assert [event["sources"][0]["sourceId"] for event in merged] == [cabinet] * 4
    assert [len(event["sources"]) for event in merged] == [2] * 4

    response = await v4_client.put(
        f"/api/v1/spaces/{space_id}/priorities/schedule",
        json={"sourceIds": [calendar, cabinet]},
    )
    assert response.status_code == 200, response.text
    preferred = await _get(v4_client, "/api/v1/schedule", **WINDOW)
    assert [event["sources"][0]["sourceId"] for event in preferred] == [calendar] * 4
    assert {event["provenance"]["sourceId"] for event in preferred} == {calendar}
    # the additional source is still shown as a contributor, second in the list
    assert [event["sources"][1]["sourceId"] for event in preferred] == [cabinet] * 4
