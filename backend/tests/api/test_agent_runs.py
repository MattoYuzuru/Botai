"""HTTP runs API of the v4 agent: enqueue, status and trace reads with ownership checks.

The API is the product's entry point into the same `AGENT` job the v3 worker has always used, so
these tests check both sides: the queue row the POST creates and the `agent_runs`/
`agent_run_events` rows the GETs read. The flag is part of the contract: on v3 every endpoint is
`404`, like the sources-v4 REST API.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from botai.core.errors import ErrorCode
from botai.core.jobs import JobKind
from botai.db.models import AgentRun, AgentRunEvent
from botai.settings import Settings, get_settings
from tests.auth.support import Sm, make_connection, make_space, signup

pytestmark = pytest.mark.db

RUNS = "/api/v1/agent/runs"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
def v4_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"agent_engine": "v4"})


@pytest.fixture
def v4_app(app: FastAPI, v4_settings: Settings) -> FastAPI:
    app.dependency_overrides[get_settings] = lambda: v4_settings
    return app


@pytest.fixture
def v4_client(v4_app: FastAPI, new_client: Callable[[], httpx.AsyncClient]) -> httpx.AsyncClient:
    return new_client()


def _run_row(user_id: str, run_id: str, *, status: str = "answer") -> AgentRun:
    return AgentRun(
        run_id=run_id,
        user_id=user_id,
        task="найди расписание",
        provider="mock",
        model="scripted",
        status=status,
        started_at=NOW,
        finished_at=NOW,
        steps=2,
        total_tokens=30,
        cost_usd=0.0001,
        event_count=3,
    )


def _events(run_id: str) -> list[AgentRunEvent]:
    return [
        AgentRunEvent(
            run_id=run_id,
            seq=seq,
            kind=kind,
            step=0,
            duration_ms=1,
            at=NOW,
            payload={"step": seq},
        )
        for seq, kind in enumerate(("run_started", "model_call", "run_finished"))
    ]


# --- POST ---


async def test_post_run_enqueues_the_agent_job_with_a_pre_generated_run_id(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: Any,
    services: Any,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    response = await v4_client.post(RUNS, json={"task": "  открой кабинет  "})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["runId"]
    assert body["jobId"]
    queued = services.queue.jobs
    assert [job["kind"] for job in queued] == [JobKind.AGENT]
    assert queued[0]["id"] == body["jobId"]
    assert queued[0]["user_id"] == account.id
    assert queued[0]["payload"]["task"] == "открой кабинет"
    assert queued[0]["payload"]["_run_id"] == body["runId"]


async def test_post_run_accepts_the_owners_thread_space_and_connection(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: Any,
    services: Any,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space = await make_space(tx_sessionmaker, account.id)
    connection = await make_connection(tx_sessionmaker, account.id, space)
    thread = await v4_client.post("/api/v1/chat/threads", json={"spaceId": space, "title": "Вуз"})
    assert thread.status_code == 201, thread.text

    response = await v4_client.post(
        RUNS,
        json={
            "task": "почини источник",
            "threadId": thread.json()["id"],
            "spaceId": space,
            "connectionId": connection,
        },
    )
    assert response.status_code == 202, response.text
    queued = services.queue.jobs[-1]
    assert queued["thread_id"] == thread.json()["id"]
    assert queued["connection_id"] == connection
    assert queued["payload"]["_space_id"] == space
    assert queued["payload"]["_connection_id"] == connection


async def test_post_run_refuses_a_foreign_connection_or_thread(
    v4_client: httpx.AsyncClient,
    new_client: Callable[[], httpx.AsyncClient],
    tx_sessionmaker: Sm,
    clock: Any,
) -> None:
    await signup(v4_client, tx_sessionmaker, clock)
    other_client = new_client()
    other = await signup(other_client, tx_sessionmaker, clock)
    space = await make_space(tx_sessionmaker, other.id)
    connection = await make_connection(tx_sessionmaker, other.id, space)
    thread = await other_client.post("/api/v1/chat/threads", json={"spaceId": space, "title": "Ч"})
    assert thread.status_code == 201, thread.text

    foreign_connection = await v4_client.post(
        RUNS, json={"task": "задача", "connectionId": connection}
    )
    assert foreign_connection.status_code == 404
    assert foreign_connection.json()["code"] == ErrorCode.NOT_FOUND.value
    foreign_thread = await v4_client.post(
        RUNS, json={"task": "задача", "threadId": thread.json()["id"]}
    )
    assert foreign_thread.status_code == 404


async def test_post_run_refuses_a_connection_from_another_space(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: Any,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space = await make_space(tx_sessionmaker, account.id)
    other_space = await make_space(tx_sessionmaker, account.id, title="Другой вуз")
    connection = await make_connection(tx_sessionmaker, account.id, space)

    response = await v4_client.post(
        RUNS,
        json={"task": "задача", "spaceId": other_space, "connectionId": connection},
    )
    assert response.status_code == 409


async def test_post_run_needs_a_login_and_a_non_empty_task(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: Any,
) -> None:
    anonymous = await v4_client.post(RUNS, json={"task": "задача"})
    assert anonymous.status_code == 401

    await signup(v4_client, tx_sessionmaker, clock)
    empty = await v4_client.post(RUNS, json={"task": "   "})
    assert empty.status_code == 422
    assert empty.json()["code"] == ErrorCode.VALIDATION.value


# --- GET ---


async def test_get_run_returns_the_owner_row(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: Any,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    async with tx_sessionmaker() as session:
        session.add(_run_row(account.id, "01M68Z000000000000000000A0"))
        await session.commit()

    response = await v4_client.get(f"{RUNS}/01M68Z000000000000000000A0")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runId"] == "01M68Z000000000000000000A0"
    assert body["jobId"] is None
    assert body["task"] == "найди расписание"
    assert body["provider"] == "mock"
    assert body["model"] == "scripted"
    assert body["status"] == "answer"
    assert datetime.fromisoformat(body["startedAt"]) == NOW
    assert datetime.fromisoformat(body["finishedAt"]) == NOW
    assert (body["steps"], body["totalTokens"], body["eventCount"]) == (2, 30, 3)
    assert body["costUsd"] == pytest.approx(0.0001)


async def test_get_run_events_pages_and_filters(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: Any,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    run_id = "01M68Z000000000000000000A1"
    async with tx_sessionmaker() as session:
        session.add(_run_row(account.id, run_id))
        session.add_all(_events(run_id))
        await session.commit()

    response = await v4_client.get(f"{RUNS}/{run_id}/events")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runId"] == run_id
    assert [event["kind"] for event in body["events"]] == [
        "run_started",
        "model_call",
        "run_finished",
    ]
    assert body["hasMore"] is False

    after = await v4_client.get(f"{RUNS}/{run_id}/events", params={"afterSeq": 1})
    assert [event["seq"] for event in after.json()["events"]] == [1, 2]

    page = await v4_client.get(f"{RUNS}/{run_id}/events", params={"limit": 2})
    assert [event["seq"] for event in page.json()["events"]] == [0, 1]
    assert page.json()["hasMore"] is True


async def test_another_users_run_is_not_found(
    v4_client: httpx.AsyncClient,
    new_client: Callable[[], httpx.AsyncClient],
    tx_sessionmaker: Sm,
    clock: Any,
) -> None:
    owner = await signup(v4_client, tx_sessionmaker, clock)
    other_client = new_client()
    await signup(other_client, tx_sessionmaker, clock)
    run_id = "01M68Z000000000000000000A2"
    async with tx_sessionmaker() as session:
        session.add(_run_row(owner.id, run_id))
        await session.commit()

    for path in (f"{RUNS}/{run_id}", f"{RUNS}/{run_id}/events"):
        response = await other_client.get(path)
        assert response.status_code == 404, path
        assert response.json()["code"] == ErrorCode.NOT_FOUND.value


# --- the feature flag ---


async def test_the_whole_api_is_404_on_the_v3_engine(
    app: FastAPI,
    new_client: Callable[[], httpx.AsyncClient],
) -> None:
    client = new_client()
    run_id = "01M68Z000000000000000000A0"
    for method, path in (
        ("post", RUNS),
        ("get", f"{RUNS}/{run_id}"),
        ("get", f"{RUNS}/{run_id}/events"),
    ):
        kwargs = {"json": {"task": "задача"}} if method == "post" else {}
        response = await getattr(client, method)(path, **kwargs)
        assert response.status_code == 404, path
        assert "BOTAI_AGENT_ENGINE=v4" in response.json()["detail"]
