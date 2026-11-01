"""The worker scheduler's v4 source planner (phase 3.3): due sources become replay jobs.

The scheduler is an optimisation of freshness: it selects due `sources` in one batch, enqueues the
same SYNC job the owner could start by hand (connection path when the source has one, a
`{source_id}` job otherwise) and never duplicates a task that is already queued/running. The
planner also owns the planning split: a connection that carries a v4 source is not planned by the
v3 connection planner, so `BOTAI_SOURCE_SYNC_INTERVAL_S` is actually honoured.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from botai.agent.v4.dispatch import make_v4_handlers
from botai.core.jobs import JobKind
from botai.db.models import Connection, Job, Source, User
from botai.jobs.runner import schedule_source_syncs, schedule_syncs
from botai.settings import get_settings
from botai.sources.v4 import playbooks, service
from botai.sources.v4.schemas import SourcePatch
from tests.auth.support import make_connection, make_space
from tests.fakeuni import api as fakeuni_api
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env
from tests.sources.v4.test_runner import SCHEDULE_SPEC

pytestmark = pytest.mark.db
BASE = "https://fakeuni.test"


@pytest.fixture(autouse=True)
def _fakeuni_state() -> Any:
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


async def _account(env: Env) -> tuple[str, str]:
    """(user_id, space_id): a tracked test user with a university space."""
    client = env.new_client()
    account = await env.account(client)
    space = await make_space(env.sm, account.id)
    return account.id, space


async def _source(
    env: Env,
    user_id: str,
    space_id: str,
    *,
    due: bool = True,
    status: str = "active",
    connection_id: str | None = None,
    playbook_id: str | None = None,
) -> str:
    async with env.sm() as session:
        source = await service.create_source(
            session,
            user_id=user_id,
            space_id=space_id,
            title="Личный кабинет",
            entry_url=f"{BASE}/app?fixture={secrets.token_hex(6)}",
            kinds=["lms", "schedule"],
            config={"connection_id": connection_id} if connection_id else {},
        )
        source = await service.update_source(
            session,
            user_id=user_id,
            source_id=source.id,
            patch=SourcePatch(playbook_id=playbook_id) if playbook_id else SourcePatch(),
        )
        await service.set_status(session, user_id=user_id, source_id=source.id, status=status)
        source.next_sync_at = (
            env.clock.now() - timedelta(minutes=1) if due else env.clock.now() + timedelta(hours=1)
        )
        await session.commit()
        return source.id


async def _sync_jobs(env: Env) -> list[Job]:
    async with env.sm() as session:
        return list(
            await session.scalars(
                select(Job).where(Job.kind == JobKind.SYNC.value).order_by(Job.created_at, Job.id)
            )
        )


async def _fresh(env: Env, user_id: str, source_id: str) -> Source:
    async with env.sm() as session:
        return await service.get_source(session, user_id=user_id, source_id=source_id)


# --- selection and enqueue ----------------------------------------------------------------------


async def test_a_due_source_with_a_connection_becomes_the_usual_connection_sync(env: Env) -> None:
    user, space = await _account(env)
    connection = await make_connection(env.sm, user, space, status="active")
    source_id = await _source(env, user, space, connection_id=connection)

    assert await schedule_source_syncs(env.services) == 1
    (job,) = await _sync_jobs(env)
    assert job.connection_id == connection
    assert job.payload == {"source_id": source_id}

    # The same task is never enqueued twice: the planner sees it in flight (and the queue dedupes).
    assert await schedule_source_syncs(env.services) == 0
    assert len(await _sync_jobs(env)) == 1


async def test_a_due_source_without_a_connection_becomes_a_source_sync_job(env: Env) -> None:
    user, space = await _account(env)
    source_id = await _source(env, user, space)

    assert await schedule_source_syncs(env.services) == 1
    (job,) = await _sync_jobs(env)
    assert job.connection_id is None
    assert job.payload == {"source_id": source_id}

    # Queue-level dedupe by `source_id` (no connection key exists for such a job).
    same = await env.services.queue.enqueue(
        JobKind.SYNC, user_id=user, payload={"source_id": source_id}, dedupe=True
    )
    assert same == job.id
    assert len(await _sync_jobs(env)) == 1


async def test_not_due_unplanned_and_disabled_sources_are_not_selected(env: Env) -> None:
    user, space = await _account(env)
    await _source(env, user, space, due=False)
    await _source(env, user, space, due=True, status="draft")
    await _source(env, user, space, due=True, status="probing")
    await _source(env, user, space, due=True, status="waiting_user")
    await _source(env, user, space, due=True, status="disabled")
    await _source(env, user, space, due=True, status="archived")

    assert await schedule_source_syncs(env.services) == 0
    assert await _sync_jobs(env) == []


async def test_a_disabled_user_gets_no_background_syncs(env: Env) -> None:
    user, space = await _account(env)
    await _source(env, user, space)
    async with env.sm() as session:
        await session.execute(update(User).where(User.id == user).values(disabled=True))
        await session.commit()

    assert await schedule_source_syncs(env.services) == 0


async def test_the_batch_limit_is_respected(env: Env) -> None:
    user, space = await _account(env)
    for _ in range(5):
        await _source(env, user, space)

    assert await schedule_source_syncs(env.services, limit=2) == 2
    # The next tick skips the two tasks in flight and schedules the rest within the new batch.
    assert await schedule_source_syncs(env.services, limit=10) == 3
    assert await schedule_source_syncs(env.services, limit=10) == 0
    assert len(await _sync_jobs(env)) == 5


async def test_switching_a_source_off_cancels_its_plan(env: Env) -> None:
    user, space = await _account(env)
    source_id = await _source(env, user, space)

    async with env.sm() as session:
        await service.disable_source(session, user_id=user, source_id=source_id)
        await session.commit()

    assert (await _fresh(env, user, source_id)).next_sync_at is None
    assert await schedule_source_syncs(env.services) == 0


# --- one planner per state model ----------------------------------------------------------------


async def test_the_connection_planner_skips_a_connection_carried_by_a_v4_source(env: Env) -> None:
    user, space = await _account(env)
    connection = await make_connection(env.sm, user, space, status="active")
    async with env.sm() as session:
        await session.execute(
            update(Connection)
            .where(Connection.id == connection)
            .values(next_sync_at=env.clock.now())
        )
        await session.commit()
    await _source(env, user, space, connection_id=connection)

    # Two due planners would mean two competing schedules for one source: the source interval wins.
    assert await schedule_syncs(env.services) == 1
    (job,) = await _sync_jobs(env)
    assert job.connection_id == connection
    assert job.payload.get("source_id")


async def test_a_plain_v3_connection_still_uses_the_connection_planner(env: Env) -> None:
    user, space = await _account(env)
    connection = await make_connection(env.sm, user, space, status="active")
    async with env.sm() as session:
        await session.execute(
            update(Connection)
            .where(Connection.id == connection)
            .values(next_sync_at=env.clock.now())
        )
        await session.commit()

    assert await schedule_syncs(env.services) == 1
    (job,) = await _sync_jobs(env)
    assert job.connection_id == connection
    assert job.payload == {}


# --- end to end: the job is a deterministic replay by source id --------------------------------


async def _source_with_playbook(env: Env, user_id: str, space_id: str) -> str:
    async with env.sm() as session:
        source = await service.create_source(
            session,
            user_id=user_id,
            space_id=space_id,
            title="Fakeuni",
            entry_url=f"{BASE}/app?fixture={secrets.token_hex(6)}",
            kinds=["lms", "schedule"],
        )
        playbook = await playbooks.create_playbook(
            session, user_id=user_id, title="Fakeuni", university_id=None
        )
        version = await playbooks.add_playbook_version(
            session,
            user_id=user_id,
            playbook_id=playbook.id,
            spec=SCHEDULE_SPEC,
            canary={"ok": True, "checked_at": env.clock.now().isoformat()},
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
        await service.set_status(session, user_id=user_id, source_id=source.id, status="active")
        source.next_sync_at = env.clock.now() - timedelta(minutes=1)
        await session.commit()
        return source.id


async def test_a_scheduled_source_without_connection_is_synced_through_the_job_path(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
) -> None:
    env.settings.agent_engine = "v4"
    user, space = await _account(env)
    source_id = await _source_with_playbook(env, user, space)

    assert await schedule_source_syncs(env.services) == 1
    (job,) = await _sync_jobs(env)
    assert job.connection_id is None
    assert job.payload == {"source_id": source_id}

    env.start(
        env.worker(
            make_v4_handlers(
                http_transport=fakeuni_transport,
                artifact_root=str(tmp_path / "artifacts"),
                source_cookies=fakeuni_cookies,
            )
        )
    )
    done = await env.wait_for(job.id, {"done"}, within=30)
    assert done.result is not None
    assert done.result["status"] == "ok", done.result
    assert done.result["source_id"] == source_id
    assert "источник без подключения" in done.result["detail"]

    fresh = await _fresh(env, user, source_id)
    assert fresh.status == "active"
    assert fresh.last_error is None
    # The runner planned the next sync itself from BOTAI_SOURCE_SYNC_INTERVAL_S — the handler and
    # the scheduler hold no second copy of that logic.
    expected = env.clock.now() + timedelta(seconds=get_settings().source_sync_interval_s)
    assert fresh.next_sync_at == expected
    # And the next scheduler tick does not re-enqueue the finished source.
    assert await schedule_source_syncs(env.services) == 0
