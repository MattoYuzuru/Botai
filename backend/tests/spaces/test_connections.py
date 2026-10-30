"""Connections: creation + `connect` job, manual sync/repair, secrets that never come back."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from botai.api.deps import get_services
from botai.core.clock import FrozenClock
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.core.queue import InMemoryJobQueue
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db.models import AdapterSpecRow, Connection
from tests.auth.support import Sm, make_connection, make_space, signup
from tests.sources.recipes import recipe_spec

Make = Callable[[], httpx.AsyncClient]
TOKEN = "tok-FIXTURE-9f3a"
PASSWORD = "P@ssw0rd-FIXTURE"


async def _space(c: httpx.AsyncClient, title: str = "Вуз") -> str:
    return (await c.post("/api/v1/spaces", json={"title": title})).json()["id"]


async def test_create_connection_starts_a_connect_job(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, queue: InMemoryJobQueue
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await _space(client)
    r = await client.post(
        f"/api/v1/spaces/{space}/connections",
        json={"kind": "lms", "url": "https://Lms.Example.org/login?next=/x", "title": "Мой LMS"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    conn = body["connection"]
    assert conn["status"] == "draft"
    assert conn["origin"] == "https://lms.example.org"
    assert conn["kind"] == "lms"
    assert conn["title"] == "Мой LMS"
    assert conn["spaceId"] == space
    assert conn["hasPassword"] is False
    assert conn["adapter"] is None
    assert conn["lastSyncAt"] is None

    assert len(queue.jobs) == 1
    job = queue.jobs[0]
    assert job["id"] == body["jobId"]
    assert job["kind"] == JobKind.CONNECT
    assert job["user_id"] == acc.id
    assert job["connection_id"] == conn["id"]
    assert job["payload"] == {
        "url": "https://lms.example.org/login?next=/x",
        "kind": "lms",
        "kinds": ["scheduleEvents", "deadlines"],
    }  # Explicit legacy selection and hints only, never credentials.

    async with tx_sessionmaker() as s:
        row = (await s.execute(select(Connection))).scalar_one()
    assert row.allow_hosts == [
        "lms.example.org"
    ]  # the agent widens it; the egress proxy enforces it


async def test_a_new_source_is_never_titled_by_its_host(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    space = await _space(client)
    r = await client.post(
        f"/api/v1/spaces/{space}/connections",
        json={"kind": "lms", "url": "https://Lms.Example.org/courses?term=3"},
    )
    conn = r.json()["connection"]
    assert conn["title"] == "Система обучения"  # no name was given: the kind's name
    assert conn["entryUrl"] == "https://lms.example.org/courses?term=3"  # the page that was given
    assert conn["threadId"] is None
    assert conn["activeTask"] is None


async def test_create_connection_without_a_url_lets_the_agent_discover(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, queue: InMemoryJobQueue
) -> None:
    await signup(client, tx_sessionmaker, clock)
    space = await _space(client)
    r = await client.post(f"/api/v1/spaces/{space}/connections", json={})
    assert r.status_code == 201
    assert r.json()["connection"]["kind"] == "other"
    assert r.json()["connection"]["title"] == "Источник"
    assert r.json()["connection"]["origin"] == ""
    assert queue.jobs[0]["kind"] == JobKind.CONNECT
    assert queue.jobs[0]["payload"] == {"kinds": ["scheduleEvents", "deadlines"]}


async def test_a_repeated_request_while_setup_runs_is_the_same_connection(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, queue: InMemoryJobQueue
) -> None:
    await signup(client, tx_sessionmaker, clock)
    space = await _space(client)
    payload = {"kind": "lms", "url": "https://lms.example.org"}
    first = (await client.post(f"/api/v1/spaces/{space}/connections", json=payload)).json()
    second = (await client.post(f"/api/v1/spaces/{space}/connections", json=payload)).json()
    assert first["connection"]["id"] == second["connection"]["id"]
    assert first["jobId"] == second["jobId"]
    assert len(queue.jobs) == 1
    async with tx_sessionmaker() as s:
        assert len((await s.execute(select(Connection))).all()) == 1


async def test_a_finished_connection_does_not_swallow_a_new_one(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, queue: InMemoryJobQueue
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await _space(client)
    await make_connection(tx_sessionmaker, acc.id, space, status="active")
    r = await client.post(
        f"/api/v1/spaces/{space}/connections",
        json={"kind": "lms", "url": "https://lms.example.org"},
    )
    assert r.status_code == 201
    async with tx_sessionmaker() as s:
        assert len((await s.execute(select(Connection))).all()) == 2


@pytest.mark.parametrize(
    "url", ["ftp://x.example.org", "https://u:p@x.example.org", "https://" + "a" * 300 + ".org"]
)
async def test_bad_urls_are_rejected_without_a_connection(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    queue: InMemoryJobQueue,
    url: str,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    space = await _space(client)
    r = await client.post(f"/api/v1/spaces/{space}/connections", json={"url": url})
    assert r.status_code == 422
    assert queue.jobs == []
    async with tx_sessionmaker() as s:
        assert (await s.execute(select(Connection))).first() is None


async def test_unknown_kind_is_a_validation_error(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    space = await _space(client)
    assert (
        await client.post(f"/api/v1/spaces/{space}/connections", json={"kind": "bank"})
    ).status_code == 422


class _BrokenQueue:
    async def enqueue(self, *_: Any, **__: Any) -> str:
        raise RuntimeError("queue down")


async def test_no_orphan_connection_when_the_job_cannot_be_queued(
    app: FastAPI, tx_sessionmaker: Sm, clock: FrozenClock, services: Services, new_client: Make
) -> None:
    c = new_client()
    await signup(c, tx_sessionmaker, clock)
    space = await _space(c)
    broken = dataclasses.replace(services, queue=_BrokenQueue())
    app.dependency_overrides[get_services] = lambda: broken
    with pytest.raises(RuntimeError, match="queue down"):
        await c.post(f"/api/v1/spaces/{space}/connections", json={"url": "https://lms.example.org"})
    async with tx_sessionmaker() as s:
        assert (await s.execute(select(Connection))).first() is None


async def test_list_and_get_connection(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, services: Services
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await _space(client)
    other = await _space(client, "Другой вуз")
    a = await make_connection(tx_sessionmaker, acc.id, space, origin="https://a.example.org")
    await make_connection(tx_sessionmaker, acc.id, other, origin="https://b.example.org")
    await services.vault.put(acc.id, a, SecretKind.PASSWORD, PASSWORD)

    listed = (await client.get(f"/api/v1/spaces/{space}/connections")).json()
    assert [c["id"] for c in listed] == [a]
    assert listed[0]["hasPassword"] is True
    got = await client.get(f"/api/v1/connections/{a}")
    assert got.status_code == 200
    assert got.json()["hasPassword"] is True
    assert PASSWORD not in got.text


async def test_delete_connection_removes_it_and_its_secrets(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, services: Services
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    space = await _space(client)
    doomed = await make_connection(tx_sessionmaker, acc.id, space)
    kept = await make_connection(tx_sessionmaker, acc.id, space, origin="https://b.example.org")
    for cid in (doomed, kept):
        await services.vault.put(acc.id, cid, SecretKind.PASSWORD, PASSWORD)
        await services.vault.put(acc.id, cid, SecretKind.SESSION, "cookie-FIXTURE")

    assert (await client.delete(f"/api/v1/connections/{doomed}")).status_code == 204
    assert (await client.get(f"/api/v1/connections/{doomed}")).status_code == 404
    assert not await services.vault.has(acc.id, doomed, SecretKind.PASSWORD)
    assert not await services.vault.has(acc.id, doomed, SecretKind.SESSION)
    assert await services.vault.has(acc.id, kept, SecretKind.PASSWORD)
    assert (await client.delete(f"/api/v1/connections/{doomed}")).status_code == 404


# --- sync / repair ------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "kind"), [("sync", JobKind.SYNC), ("repair", JobKind.REPAIR)])
@pytest.mark.parametrize("status", ["active", "stale", "needs_reauth", "error", "syncing"])
async def test_sync_and_repair_enqueue_a_deduplicated_job(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    queue: InMemoryJobQueue,
    path: str,
    kind: JobKind,
    status: str,
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    conn = await make_connection(tx_sessionmaker, acc.id, await _space(client), status=status)
    r1 = await client.post(f"/api/v1/connections/{conn}/{path}")
    r2 = await client.post(f"/api/v1/connections/{conn}/{path}")
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["jobId"] == r2.json()["jobId"]
    if status == "error":
        async with tx_sessionmaker() as session:
            row = await session.get(Connection, conn)
            assert row is not None
            assert row.adapter is None
            assert row.spec_id is None
    expected = JobKind.CONNECT if status == "error" else kind
    assert [(j["kind"], j["connection_id"], j["user_id"]) for j in queue.jobs] == [
        (expected, conn, acc.id)
    ]


@pytest.mark.parametrize("path", ["sync", "repair"])
@pytest.mark.parametrize("status", ["draft", "learning", "disabled"])
async def test_sync_and_repair_wait_for_a_finished_setup(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    queue: InMemoryJobQueue,
    path: str,
    status: str,
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    conn = await make_connection(tx_sessionmaker, acc.id, await _space(client), status=status)
    r = await client.post(f"/api/v1/connections/{conn}/{path}")
    if status != "disabled":
        assert r.status_code == 202
        assert [(j["kind"], j["connection_id"]) for j in queue.jobs] == [(JobKind.CONNECT, conn)]
    else:
        assert r.status_code == 409
        assert r.json()["code"] == "conflict"
        assert queue.jobs == []


@pytest.mark.parametrize("path", ["sync", "repair"])
async def test_resume_setup_reuses_its_waiting_connect_job(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    queue: InMemoryJobQueue,
    path: str,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    started = await client.post(
        f"/api/v1/spaces/{await _space(client)}/connections",
        json={"url": "https://fakeuni.test/"},
    )
    assert started.status_code == 201
    body = started.json()
    resumed = await client.post(f"/api/v1/connections/{body['connection']['id']}/{path}")
    assert resumed.status_code == 202
    assert resumed.json()["jobId"] == body["jobId"]
    assert len(queue.jobs) == 1


@pytest.mark.parametrize(("path", "kind"), [("sync", JobKind.SYNC), ("repair", JobKind.REPAIR)])
async def test_failed_configured_connection_still_syncs_its_adapter(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    queue: InMemoryJobQueue,
    path: str,
    kind: JobKind,
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    conn = await make_connection(
        tx_sessionmaker, acc.id, await _space(client), status="error", origin="https://fakeuni.test"
    )
    async with tx_sessionmaker() as session:
        row = await session.get(Connection, conn)
        assert row is not None
        spec = AdapterSpecRow(
            id=new_id(),
            origin=row.origin,
            adapter="recipe",
            revision=1,
            state="active",
            spec=recipe_spec(),
        )
        session.add(spec)
        await session.flush()
        row.adapter = "recipe"
        row.spec_id = spec.id
        row.spec_revision = 1
        await session.commit()
    response = await client.post(f"/api/v1/connections/{conn}/{path}")
    assert response.status_code == 202
    assert [(j["kind"], j["connection_id"], j["user_id"]) for j in queue.jobs] == [
        (kind, conn, acc.id)
    ]


# --- secrets ------------------------------------------------------------------------------------


async def test_put_secret_goes_to_the_vault_and_never_comes_back(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, services: Services
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    conn = await make_connection(tx_sessionmaker, acc.id, await _space(client))
    r = await client.put(
        f"/api/v1/connections/{conn}/secrets", json={"kind": "token", "value": TOKEN}
    )
    assert r.status_code == 204
    assert r.content == b""
    stored = await services.vault.get(acc.id, conn, SecretKind.TOKEN)
    assert stored is not None
    assert stored.get_secret_value() == TOKEN
    # nothing the API says afterwards contains it
    for path in (f"/api/v1/connections/{conn}", "/api/v1/spaces", "/api/v1/me"):
        assert TOKEN not in (await client.get(path)).text
    # only the kind is reported back, so the UI can say "токен сохранён"
    assert (await client.get(f"/api/v1/connections/{conn}")).json()["storedSecrets"] == ["token"]


async def test_put_secret_replaces_the_previous_value(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, services: Services
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    conn = await make_connection(tx_sessionmaker, acc.id, await _space(client))
    for value in ("old-token", "new-token"):
        await client.put(
            f"/api/v1/connections/{conn}/secrets", json={"kind": "api_key", "value": value}
        )
    got = await services.vault.get(acc.id, conn, SecretKind.API_KEY)
    assert got is not None
    assert got.get_secret_value() == "new-token"


@pytest.mark.parametrize(
    "value", ["https://cal.example.org/feed.ics", "webcal://cal.example.org/feed.ics"]
)
async def test_ical_url_accepts_calendar_links(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    services: Services,
    value: str,
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    conn = await make_connection(tx_sessionmaker, acc.id, await _space(client), kind="schedule")
    r = await client.put(
        f"/api/v1/connections/{conn}/secrets", json={"kind": "ical_url", "value": value}
    )
    assert r.status_code == 204
    assert await services.vault.has(acc.id, conn, SecretKind.ICAL_URL)


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "ical_url", "value": "javascript:alert(1)"},
        {"kind": "ical_url", "value": "file:///etc/passwd"},
        {"kind": "token", "value": "   "},
        {"kind": "password", "value": "x"},  # passwords go through the login flow, not here
        {"kind": "session", "value": "x"},
        {"kind": "token", "value": ""},
    ],
)
async def test_put_secret_validation(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    services: Services,
    body: dict[str, str],
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    conn = await make_connection(tx_sessionmaker, acc.id, await _space(client))
    r = await client.put(f"/api/v1/connections/{conn}/secrets", json=body)
    assert r.status_code == 422
    for kind in SecretKind:
        assert not await services.vault.has(acc.id, conn, kind)


async def test_forget_password_removes_password_and_login_only(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, services: Services
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    conn = await make_connection(tx_sessionmaker, acc.id, await _space(client))
    for kind in (SecretKind.PASSWORD, SecretKind.LOGIN, SecretKind.TOKEN, SecretKind.SESSION):
        await services.vault.put(acc.id, conn, kind, f"{kind.value}-FIXTURE")
    assert (await client.get(f"/api/v1/connections/{conn}")).json()["hasPassword"] is True

    assert (await client.delete(f"/api/v1/connections/{conn}/password")).status_code == 204
    assert (await client.get(f"/api/v1/connections/{conn}")).json()["hasPassword"] is False
    assert not await services.vault.has(acc.id, conn, SecretKind.PASSWORD)
    assert not await services.vault.has(acc.id, conn, SecretKind.LOGIN)
    assert await services.vault.has(acc.id, conn, SecretKind.TOKEN)  # other secrets stay
    assert await services.vault.has(acc.id, conn, SecretKind.SESSION)
    assert (
        await client.delete(f"/api/v1/connections/{conn}/password")
    ).status_code == 204  # idempotent


async def test_secret_values_do_not_reach_the_logs(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    conn = await make_connection(tx_sessionmaker, acc.id, await _space(client))
    with caplog.at_level(logging.DEBUG):
        await client.put(
            f"/api/v1/connections/{conn}/secrets", json={"kind": "token", "value": TOKEN}
        )
        await client.put(
            f"/api/v1/connections/{conn}/secrets",
            json={"kind": "ical_url", "value": "ftp://" + TOKEN},
        )
    assert TOKEN not in caplog.text


# --- tenant isolation ---------------------------------------------------------------------------


async def test_user_b_cannot_reach_user_a_connections(
    new_client: Make,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    services: Services,
    queue: InMemoryJobQueue,
) -> None:
    a, b = new_client(), new_client()
    alice = await signup(a, tx_sessionmaker, clock, email="a@example.com")
    await signup(b, tx_sessionmaker, clock, email="b@example.com")
    conn = await make_connection(tx_sessionmaker, alice.id, await _space(a))
    await services.vault.put(alice.id, conn, SecretKind.PASSWORD, PASSWORD)
    await services.vault.put(alice.id, conn, SecretKind.TOKEN, TOKEN)

    attempts: list[tuple[str, str, dict[str, Any]]] = [
        ("GET", f"/api/v1/connections/{conn}", {}),
        ("DELETE", f"/api/v1/connections/{conn}", {}),
        ("POST", f"/api/v1/connections/{conn}/sync", {}),
        ("POST", f"/api/v1/connections/{conn}/repair", {}),
        (
            "PUT",
            f"/api/v1/connections/{conn}/secrets",
            {"json": {"kind": "token", "value": "hijack"}},
        ),
        ("DELETE", f"/api/v1/connections/{conn}/password", {}),
    ]
    for method, path, kwargs in attempts:
        r = await b.request(method, path, **kwargs)
        assert r.status_code == 404, (method, path)
        assert r.json()["code"] == "not_found"
    ghost = await b.get(f"/api/v1/connections/{new_id()}")
    assert ghost.json()["detail"] == (await b.get(f"/api/v1/connections/{conn}")).json()["detail"]

    # Alice's connection, secrets and queue are exactly as before
    assert queue.jobs == []
    assert (await a.get(f"/api/v1/connections/{conn}")).json()["hasPassword"] is True
    token = await services.vault.get(alice.id, conn, SecretKind.TOKEN)
    assert token is not None
    assert token.get_secret_value() == TOKEN


async def test_a_space_id_of_another_user_cannot_host_my_connection(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock, queue: InMemoryJobQueue
) -> None:
    a, b = new_client(), new_client()
    await signup(a, tx_sessionmaker, clock, email="a@example.com")
    bob = await signup(b, tx_sessionmaker, clock, email="b@example.com")
    alices_space = await _space(a)
    r = await b.post(
        f"/api/v1/spaces/{alices_space}/connections", json={"url": "https://x.example.org"}
    )
    assert r.status_code == 404
    assert queue.jobs == []
    assert bob.id
    other = await make_space(tx_sessionmaker, bob.id)
    assert (await b.get(f"/api/v1/spaces/{other}/connections")).json() == []


async def test_every_connection_route_needs_a_session(client: httpx.AsyncClient) -> None:
    cid = new_id()
    for method, path in (
        ("GET", "/api/v1/spaces"),
        ("POST", "/api/v1/spaces"),
        ("GET", f"/api/v1/spaces/{cid}/connections"),
        ("GET", f"/api/v1/connections/{cid}"),
        ("POST", f"/api/v1/connections/{cid}/sync"),
        ("PUT", f"/api/v1/connections/{cid}/secrets"),
        ("DELETE", f"/api/v1/connections/{cid}/password"),
    ):
        assert (await client.request(method, path)).status_code == 401, (method, path)
