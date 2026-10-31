"""Fakeuni L1 through the real job path with the v4 engine: CONNECT, SYNC and REPAIR.

No real network and no real model: the university is the synthetic `fakeuni`, the model is a
scripted provider that reacts to what the runtime actually did, and the jobs go through the real
`Worker`/Postgres queue. These tests are the product-path proof: a `CONNECT` task lets the v4 agent
research a cabinet with `source_*` tools, write and activate a playbook, sync it, leave a source
record + artifacts + a DB trace, and ask the queue for the follow-up sync; a `SYNC` replays the
saved playbook without any model call; a `REPAIR` falls back to the agent only after the
deterministic replay failed and then records the fixed playbook.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from botai.agent.v4.dispatch import make_v4_handlers
from botai.core.jobs import JobKind
from botai.core.llm import LlmRequest, LlmResponse
from botai.db.models import (
    AgentRun,
    AgentRunEvent,
    Connection,
    Job,
    Source,
    SourceArtifact,
)
from botai.sources.v4 import playbooks, service
from botai.sources.v4.schemas import SourcePatch
from tests.agent.v4.runtime_support import answer, call, tool_response
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env

pytestmark = pytest.mark.db

BASE = "https://fakeuni.test"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

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
                "courseId": "/course",
                "state": "/status",
            },
        },
    ],
    "timeout_s": 10,
}
MOVED_SCHEDULE_SPEC: dict[str, Any] = {
    "version": 1,
    "reads": [
        {
            "name": "timetable",
            "kind": "schedule",
            "url": f"{BASE}/api/v3/schedule",
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


class FlowProvider:
    """A provider whose answer is computed from the run so far (the source id is not known ahead)."""

    name = "flow"

    def __init__(self, respond: Callable[[LlmRequest], Awaitable[LlmResponse]]) -> None:
        self._respond = respond
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return await self._respond(request)

    async def aclose(self) -> None:
        return None


def _tool_calls(request: LlmRequest) -> int:
    return sum(
        1 for message in request.messages if message.role == "assistant" and message.tool_calls
    )


def _arg_text(request: LlmRequest) -> str:
    return "\n".join(message.content for message in request.messages if message.role == "tool")


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


async def _connection(env: Env, user_id: str) -> tuple[str, str]:
    space, connection_id = await env.connection(user_id, status="draft", origin=BASE, kind="lms")
    return space, connection_id


async def _source_of_connection(env: Env, connection_id: str) -> Source | None:
    async with env.sm() as session:
        return await session.scalar(
            select(Source)
            .where(Source.config["connection_id"].as_string() == connection_id)
            .order_by(Source.created_at, Source.id)
            .limit(1)
        )


async def _wait_job(env: Env, *, kind: str, connection_id: str, within: float = 30.0) -> Job:
    deadline = asyncio.get_running_loop().time() + within
    while True:
        async with env.sm() as session:
            job = await session.scalar(
                select(Job)
                .where(Job.kind == kind, Job.connection_id == connection_id)
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(1)
            )
        if job is not None and job.state in {"done", "failed", "cancelled"}:
            return job
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"no terminal {kind} job for {connection_id}: {job}")
        await asyncio.sleep(0.02)


async def _make_source_with_playbook(
    env: Env, user_id: str, connection_id: str, space_id: str, spec: dict[str, Any]
) -> str:
    async with env.sm() as session:
        source = await service.create_source(
            session,
            user_id=user_id,
            space_id=space_id,
            title="Личный кабинет",
            entry_url=f"{BASE}/app",
            kinds=["lms", "schedule", "deadlines"],
            config={"connection_id": connection_id},
        )
        playbook = await playbooks.create_playbook(
            session, user_id=user_id, title="Fakeuni", university_id=None
        )
        version = await playbooks.add_playbook_version(
            session,
            user_id=user_id,
            playbook_id=playbook.id,
            spec=spec,
            canary={"ok": True, "checked_at": NOW.isoformat()},
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
        return source.id


# --- CONNECT ------------------------------------------------------------------------------------


async def test_connect_researches_fakeuni_with_source_tools_and_queues_the_first_sync(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    space_id, connection_id = await _connection(env, user.id)

    async def respond(request: LlmRequest) -> LlmResponse:
        source = await _source_of_connection(env, connection_id)
        assert source is not None, "the connect handler must create the v4 source before the run"
        source_id = source.id
        step = _tool_calls(request)
        if step == 0:
            return tool_response(call("source_list", {}, "c1"))
        if step == 1:
            assert source_id in _arg_text(request)  # the model sees the source it was given
            return tool_response(
                call(
                    "source_probe",
                    {"source_id": source_id, "spec": SCHEDULE_SPEC, "persist": False},
                    "c2",
                )
            )
        if step == 2:
            return tool_response(
                call(
                    "source_update_playbook",
                    {"source_id": source_id, "spec": SCHEDULE_SPEC, "activate": True},
                    "c3",
                )
            )
        if step == 3:
            return tool_response(call("source_sync", {"source_id": source_id}, "c4"))
        return answer("Подключил: расписание и дедлайны читаются.")

    provider = FlowProvider(respond)
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT,
        user_id=user.id,
        connection_id=connection_id,
        payload={
            "kinds": ["scheduleEvents", "deadlines"],
            "_browser": False,
            "_workspace_root": str(tmp_path / "ws"),
        },
    )
    env.start(
        env.worker(
            make_v4_handlers(
                provider=provider,
                http_transport=fakeuni_transport,
                artifact_root=tmp_path / "artifacts",
                source_cookies=fakeuni_cookies,
            )
        )
    )
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert job.result is not None
    assert job.result["status"] == "connected", job.result
    assert job.result["source_id"]
    assert job.result["connection_id"] == connection_id
    # The agent answered in the thread as a text block with source cards.
    block_events = [event for event in await env.events(job_id) if event.type == "block"]
    assert block_events, "the connect must post its answer as a UI block"
    block = block_events[-1].data
    assert block["type"] == "text"
    assert block["sources"], "the answer must carry source cards"
    assert any(card["title"] for card in block["sources"])

    # Phases were reported forward-only: research first, sync when the playbook was replayed.
    stages = [
        str(event.data.get("stage") or "")
        for event in await env.events(job_id)
        if event.type == "status"
    ]
    assert any("исследование" in stage for stage in stages), stages
    assert any("синхронизация" in stage for stage in stages), stages

    # The v4 source is attached to the user's space and connected.
    source = await _source_of_connection(env, connection_id)
    assert source is not None
    assert source.user_id == user.id
    assert source.space_id == space_id
    assert source.playbook_id is not None
    assert source.status == "active"
    assert source.last_sync_at is not None

    # The DB trace of the run exists: one row and its steps.
    async with env.sm() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.user_id == user.id))
        assert run is not None
        assert run.status == "answer"
        assert run.job_id == job_id
        events = list(
            await session.scalars(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run.run_id)
                .order_by(AgentRunEvent.seq)
            )
        )
        assert events
        assert {"run_started", "model_call", "tool_call", "tool_result", "run_finished"} <= {
            event.kind for event in events
        }
        artifacts = list(
            await session.scalars(
                select(SourceArtifact).where(SourceArtifact.source_id == source.id)
            )
        )
        assert artifacts, "the sync through the job path must persist artifacts"
        # The run's first model request contained the actual instruction, never only a checkpoint.
        assert provider.requests
        assert any(
            "Подключи источник" in message.content
            for message in provider.requests[0].messages
            if message.role == "user"
        )

    # The queue got the follow-up sync after the connect finished (not while it held the source).
    sync_job = await _wait_job(env, kind=JobKind.SYNC.value, connection_id=connection_id)
    assert sync_job.state == "done", sync_job.error
    assert sync_job.id != job_id
    assert sync_job.result is not None
    assert sync_job.result["status"] == "ok"
    assert sync_job.result["counts"]["schedule"] > 0

    # The v3 connection row follows the v4 source (both screens agree on freshness).
    async with env.sm() as session:
        row = await session.get(Connection, connection_id)
        assert row is not None
        assert row.status == "active"
        assert row.last_sync_at is not None
        assert row.next_sync_at is not None


# --- SYNC ---------------------------------------------------------------------------------------


async def test_sync_replays_the_saved_playbook_without_a_model(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    space_id, connection_id = await _connection(env, user.id)
    source_id = await _make_source_with_playbook(
        env, user.id, connection_id, space_id, SCHEDULE_SPEC
    )

    provider = FlowProvider(lambda request: _unexpected(request))
    job_id = await env.services.queue.enqueue(
        JobKind.SYNC, user_id=user.id, connection_id=connection_id, payload={}
    )
    env.start(
        env.worker(
            make_v4_handlers(
                provider=provider,
                http_transport=fakeuni_transport,
                artifact_root=tmp_path / "artifacts",
                source_cookies=fakeuni_cookies,
            )
        )
    )
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert provider.requests == [], "the deterministic sync must not call the model"
    assert job.result is not None
    assert job.result["status"] == "ok", job.result
    assert job.result["source_id"] == source_id
    assert job.result["counts"]["schedule"] > 0

    source = await _source_of_connection(env, connection_id)
    assert source is not None
    assert source.id == source_id
    assert source.status == "active"
    assert source.last_sync_at is not None

    async with env.sm() as session:
        artifacts = list(
            await session.scalars(
                select(SourceArtifact).where(SourceArtifact.source_id == source_id)
            )
        )
        assert artifacts
        connection = await session.get(Connection, connection_id)
        assert connection is not None
        assert connection.status == "active"
        assert connection.last_sync_at is not None
        assert connection.next_sync_at is not None


async def _unexpected(request: LlmRequest) -> LlmResponse:
    raise AssertionError(f"the model must not be called here: {request.messages}")


# --- REPAIR -------------------------------------------------------------------------------------


async def test_repair_falls_back_to_the_agent_and_activates_the_fixed_playbook(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
) -> None:
    env.settings.agent_engine = "v4"
    user = await env.account()
    space_id, connection_id = await _connection(env, user.id)
    source_id = await _make_source_with_playbook(
        env, user.id, connection_id, space_id, SCHEDULE_SPEC
    )
    fakeuni_api.STATE.timetable_version = "v3"  # the endpoint moved: the saved playbook drifts

    async def respond(request: LlmRequest) -> LlmResponse:
        step = _tool_calls(request)
        if step == 0:
            return tool_response(
                call(
                    "source_read_live",
                    {"source_id": source_id, "url": f"{BASE}/api/v3/schedule"},
                    "c1",
                )
            )
        if step == 1:
            return tool_response(
                call(
                    "source_update_playbook",
                    {"source_id": source_id, "spec": MOVED_SCHEDULE_SPEC, "activate": True},
                    "c2",
                )
            )
        if step == 2:
            return tool_response(call("source_sync", {"source_id": source_id}, "c3"))
        return answer("Починил: расписание теперь читается с /api/v3/schedule.")

    provider = FlowProvider(respond)
    job_id = await env.services.queue.enqueue(
        JobKind.REPAIR,
        user_id=user.id,
        connection_id=connection_id,
        payload={
            "reason": "drift",
            "problems": [{"code": "recipe_drift", "detail": "timetable: 404"}],
            "_browser": False,
            "_workspace_root": str(tmp_path / "ws"),
        },
    )
    env.start(
        env.worker(
            make_v4_handlers(
                provider=provider,
                http_transport=fakeuni_transport,
                artifact_root=tmp_path / "artifacts",
                source_cookies=fakeuni_cookies,
            )
        )
    )
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert job.result is not None
    assert job.result["status"] in {"ok", "active"}, job.result
    assert job.result["source_id"] == source_id

    # The agent updated the playbook: a new active version that reads the moved endpoint.
    source = await _source_of_connection(env, connection_id)
    assert source is not None
    assert source.playbook_id is not None
    async with env.sm() as session:
        active = await playbooks.active_playbook_version(
            session, user_id=user.id, playbook_id=source.playbook_id
        )
        assert active is not None
        assert active.version >= 2
        assert active.spec is not None
        assert "/api/v3/schedule" in str(active.spec)
        versions = await playbooks.list_playbook_versions(
            session, user_id=user.id, playbook_id=source.playbook_id
        )
        assert len(versions) >= 2
    assert source.status == "active"

    # A successful repair queues a sync that records the fixed read (and no model call for it).
    sync_job = await _wait_job(env, kind=JobKind.SYNC.value, connection_id=connection_id)
    assert sync_job.state == "done", sync_job.error
    assert sync_job.result is not None
    assert sync_job.result["status"] == "ok"
