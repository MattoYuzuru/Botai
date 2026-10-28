"""A native login question cannot receive a secret after control moved to its browser."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from botai.contracts.agent_ui import InputBlock, LoginBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobKind
from botai.core.vault import SecretKind
from botai.db.models import Job
from botai.jobs.answers import record_answer
from botai.jobs.events import append_event
from botai.jobs.schemas import AnswerIn
from tests.auth.support import NOW
from tests.jobs.env import Env

pytestmark = pytest.mark.db


@pytest.mark.parametrize(
    "case",
    [
        "password",
        "username",
        "code",
        "url",
        "url_with_username",
        "other_connection",
        "new_question",
    ],
)
async def test_only_superseded_auth_questions_reject_late_answers(env: Env, case: str) -> None:
    client = env.new_client()
    account = await env.account(client)
    _, connection = await env.connection(account.id)
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=account.id, connection_id=connection
    )
    native = InputBlock(
        id=new_id(),
        at=NOW,
        question="Пароль для входа",
        input_kind="password",
        secret=True,
        autocomplete="current-password",
        required=True,
    )
    if case == "username":
        native = native.model_copy(
            update={"input_kind": "email", "secret": False, "autocomplete": "username"}
        )
    elif case == "code":
        native = native.model_copy(update={"input_kind": "code", "autocomplete": "one-time-code"})
    elif case in {"url", "url_with_username"}:
        native = native.model_copy(
            update={
                "input_kind": "url",
                "secret": False,
                "autocomplete": "username" if case == "url_with_username" else "url",
            }
        )
    screen = LoginBlock(
        id=new_id(),
        at=NOW,
        connection_id=new_id() if case == "other_connection" else connection,
        ticket="fixture-ticket-" + "x" * 20,
        ws_url=f"wss://botai.test/api/v1/login-sessions/{new_id()}/ws",
        expires_at=NOW + timedelta(minutes=10),
    )
    async with env.sm() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.state = "waiting_user"
        blocks = [screen, native] if case == "new_question" else [native, screen]
        for block in blocks:
            await append_event(
                session,
                job_id,
                "block",
                block.model_dump(mode="json", by_alias=True, exclude_none=True),
            )
        await session.commit()

    value = "https://fakeuni.test/" if case.startswith("url") else "fixture-only-answer"
    response = await client.post(
        f"/api/v1/jobs/{job_id}/answers", json={"blockId": native.id, "values": value}
    )
    if case in {"password", "username", "code"}:
        assert response.status_code == 409
        assert response.json()["code"] == "block_answered"
        assert await env.answers(job_id) == []
        assert not await env.services.vault.has(account.id, native.id, SecretKind.TRANSIENT)
    else:
        assert response.status_code == 204
        assert len(await env.answers(job_id)) == 1
        assert (
            await env.services.vault.has(account.id, native.id, SecretKind.TRANSIENT)
            is native.secret
        )


async def test_handoff_and_late_secret_answer_are_serialized_on_the_job(env: Env) -> None:
    account = await env.account()
    _, connection = await env.connection(account.id)
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=account.id, connection_id=connection
    )
    native = InputBlock(
        id=new_id(),
        at=NOW,
        question="Пароль для входа",
        input_kind="password",
        secret=True,
        autocomplete="current-password",
        required=True,
    )
    screen = LoginBlock(
        id=new_id(),
        at=NOW,
        connection_id=connection,
        ticket="fixture-ticket-" + "x" * 20,
        ws_url=f"wss://botai.test/api/v1/login-sessions/{new_id()}/ws",
        expires_at=NOW + timedelta(minutes=10),
    )
    async with env.sm() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.state = "waiting_user"
        await append_event(
            session,
            job_id,
            "block",
            native.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        await session.commit()

    task: asyncio.Task[None] | None = None
    async with env.sm() as blocker, env.sm() as answer, env.sm() as observer:
        try:
            await blocker.scalar(select(Job).where(Job.id == job_id).with_for_update())
            await append_event(
                blocker,
                job_id,
                "block",
                screen.model_dump(mode="json", by_alias=True, exclude_none=True),
            )
            # Pin this answer transaction to a known PostgreSQL backend. The
            # observer proves it really waits on the blocker rather than relying
            # on an arbitrary sleep or on the task merely being scheduled.
            pid = await answer.scalar(select(func.pg_backend_pid()))
            assert isinstance(pid, int)
            task = asyncio.create_task(
                record_answer(
                    answer,
                    env.services.vault,
                    account.id,
                    job_id,
                    AnswerIn(block_id=native.id, values="fixture-only-answer"),
                )
            )
            async with asyncio.timeout(5.0):
                while True:
                    waiting = await observer.scalar(
                        select(func.cardinality(func.pg_blocking_pids(pid)))
                    )
                    if waiting is not None and waiting > 0:
                        break
                    if task.done():
                        await task
                        pytest.fail("The answer completed before the handoff lock was released")
                    await asyncio.sleep(0.01)
            await blocker.commit()
            with pytest.raises(BotaiError) as exc:
                await asyncio.wait_for(task, timeout=5.0)
            assert exc.value.code == ErrorCode.BLOCK_ANSWERED
            assert exc.value.status == 409
        finally:
            await blocker.rollback()
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await answer.rollback()
    assert await env.answers(job_id) == []
    assert not await env.services.vault.has(account.id, native.id, SecretKind.TRANSIENT)
