"""The source run history endpoint and the source thread link (`SourceOut.threadId`).

Jobs reach a source three ways: the linked v3 connection of the v4 dispatch, the job payload
(`source_id`), and the run that saved the source's artifacts. The endpoint is owner-only, paginated
and reads the agent trace id of a run in one extra query.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from botai.core.ids import new_id
from botai.db.models import AgentRun, Job, SourceArtifact
from botai.sources.v4 import service as v4_service
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import FrozenClock, Sm, make_connection, signup

pytestmark = pytest.mark.db
BASE = "https://fakeuni.test"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


async def _space(client: httpx.AsyncClient, title: str = "Fakeuni") -> str:
    response = await client.post("/api/v1/spaces", json={"title": title})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _make_job(
    sm: Sm,
    user_id: str,
    *,
    kind: str = "sync",
    state: str = "done",
    source_id: str | None = None,
    connection_id: str | None = None,
    thread_id: str | None = None,
    result: dict | None = None,
    error: dict | None = None,
    created_at: datetime = NOW,
) -> str:
    job_id = new_id()
    async with sm() as session:
        session.add(
            Job(
                id=job_id,
                user_id=user_id,
                kind=kind,
                state=state,
                connection_id=connection_id,
                thread_id=thread_id,
                payload={"source_id": source_id} if source_id else {},
                result=result,
                error=error,
                run_after=NOW,
                created_at=created_at,
                started_at=created_at,
                finished_at=created_at + timedelta(minutes=1) if state == "done" else None,
            )
        )
        await session.commit()
    return job_id


async def _thread(sm: Sm, user_id: str) -> str:
    from botai.db.models import ChatThread

    thread_id = new_id()
    async with sm() as session:
        session.add(ChatThread(id=thread_id, user_id=user_id, title="Чат"))
        await session.commit()
    return thread_id


async def _runs(client: httpx.AsyncClient, source_id: str, **params: str) -> list[dict]:
    response = await client.get(f"/api/v1/sources/{source_id}/runs", params=params)
    assert response.status_code == 200, response.text
    return list(response.json())


async def test_an_unknown_source_and_a_foreign_source_are_not_found(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(v4_client, tx_sessionmaker, clock)
    missing = await v4_client.get("/api/v1/sources/01JNOSUCHSOURCE/runs")
    assert missing.status_code == 404
    # another student's source is exactly "not found", not forbidden
    from tests.sources.v4.support import create_account

    other = await create_account(tx_sessionmaker)
    async with tx_sessionmaker() as session:
        foreign = await v4_service.create_source(
            session, user_id=other.user_id, space_id=other.space_id, title="Чужой"
        )
        await session.commit()
    response = await v4_client.get(f"/api/v1/sources/{foreign.id}/runs")
    assert response.status_code == 404


async def test_the_runs_of_a_source_are_newest_first_with_their_outcome(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    source = (
        await v4_client.post(
            "/api/v1/sources",
            json={"spaceId": space_id, "title": "ЛК Fakeuni", "url": f"{BASE}/app"},
        )
    ).json()

    older = await _make_job(
        tx_sessionmaker,
        account.id,
        kind="connect",
        state="done",
        source_id=source["id"],
        created_at=NOW - timedelta(hours=2),
    )
    newer = await _make_job(
        tx_sessionmaker,
        account.id,
        kind="sync",
        state="failed",
        source_id=source["id"],
        result={"counts": {"schedule": 4}},
        error={"code": "read_failed", "detail": "страница изменилась"},
        created_at=NOW,
    )
    # a job of another user that names the same source must not leak in
    from tests.sources.v4.support import create_account

    other = await create_account(tx_sessionmaker)
    await _make_job(tx_sessionmaker, other.user_id, source_id=source["id"], created_at=NOW)

    rows = await _runs(v4_client, source["id"])
    assert [row["id"] for row in rows] == [newer, older]
    failed, connected = rows
    assert (failed["kind"], failed["status"], failed["ok"]) == ("sync", "failed", False)
    assert failed["counts"] == {"schedule": 4}
    assert (failed["errorCode"], failed["errorDetail"]) == (
        "read_failed",
        "страница изменилась",
    )
    assert failed["startedAt"] is not None
    assert failed["finishedAt"] is None
    assert (connected["kind"], connected["status"], connected["ok"]) == ("connect", "done", True)
    assert connected["counts"] == {}
    assert connected["runId"] is None

    # pagination: one row per page, newest first
    assert len(await _runs(v4_client, source["id"], limit="1")) == 1
    assert [row["id"] for row in await _runs(v4_client, source["id"], limit="1", offset="1")] == [
        older
    ]
    assert await _runs(v4_client, source["id"], offset="2") == []


async def test_a_run_is_linked_by_the_connection_the_artifact_and_the_agent_trace(
    v4_client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    account = await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    created = (
        await v4_client.post(
            "/api/v1/sources",
            json={"spaceId": space_id, "title": "ЛК Fakeuni", "url": f"{BASE}/app"},
        )
    ).json()
    connection_id = await make_connection(tx_sessionmaker, account.id, space_id)
    async with tx_sessionmaker() as session:
        await v4_service.update_source(
            session,
            user_id=account.id,
            source_id=created["id"],
            patch=SourcePatch(config={"connection_id": connection_id}),
        )
        await session.commit()
    thread_id = await _thread(tx_sessionmaker, account.id)

    # linked by the dispatch's connection: its thread is the source's thread too
    connected_job = await _make_job(
        tx_sessionmaker,
        account.id,
        kind="sync",
        connection_id=connection_id,
        thread_id=thread_id,
        created_at=NOW - timedelta(minutes=10),
    )
    # linked only by the artifact it saved
    artifact_job = await _make_job(
        tx_sessionmaker,
        account.id,
        kind="repair",
        source_id=created["id"],
        created_at=NOW - timedelta(minutes=5),
    )
    async with tx_sessionmaker() as session:
        session.add(
            SourceArtifact(
                id=new_id(),
                user_id=account.id,
                source_id=created["id"],
                kind="page",
                sha256="0" * 64,
                run_id=artifact_job,
            )
        )
        await session.commit()
    # linked by the agent trace of its job
    agent_job = await _make_job(
        tx_sessionmaker, account.id, kind="sync", source_id=created["id"], created_at=NOW
    )
    run_id = new_id()
    async with tx_sessionmaker() as session:
        session.add(
            AgentRun(
                run_id=run_id,
                user_id=account.id,
                job_id=agent_job,
                task="sync",
                status="done",
            )
        )
        await session.commit()

    rows = await _runs(v4_client, created["id"])
    assert [row["id"] for row in rows] == [agent_job, artifact_job, connected_job]
    assert rows[0]["runId"] == run_id
    assert [row["threadId"] for row in rows] == [None, None, thread_id]

    # the source card gets the thread of its latest task, in one query for the whole list
    card = (await v4_client.get(f"/api/v1/sources/{created['id']}")).json()
    assert card["threadId"] == thread_id
    listed = (await v4_client.get("/api/v1/sources", params={"space_id": space_id})).json()
    assert [row["threadId"] for row in listed] == [thread_id]


async def test_a_source_without_tasks_has_no_thread_and_no_runs(
    v4_client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(v4_client, tx_sessionmaker, clock)
    space_id = await _space(v4_client)
    source = (
        await v4_client.post(
            "/api/v1/sources",
            json={"spaceId": space_id, "title": "ЛК Fakeuni", "url": f"{BASE}/app"},
        )
    ).json()
    assert source["threadId"] is None
    assert await _runs(v4_client, source["id"]) == []


async def test_the_runs_endpoint_is_off_on_the_v3_engine(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    from tests.sources.v4.support import create_account

    account = await create_account(tx_sessionmaker)
    async with tx_sessionmaker() as session:
        source = await v4_service.create_source(
            session, user_id=account.user_id, space_id=account.space_id, title="ЛК"
        )
        await session.commit()
    response = await client.get(f"/api/v1/sources/{source.id}/runs")
    assert response.status_code == 404
    assert "v4" in response.json()["detail"]
