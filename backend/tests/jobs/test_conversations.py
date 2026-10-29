"""Durable conversational source setup (FR-004/007/016/022/030), real PG, no live sites."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.tools import ChatEnv, request_login, request_repair
from botai.contracts.agent_ui import InputBlock, LoginBlock, TextBlock, ThinkingBlock
from botai.core.ids import new_id
from botai.core.jobs import JobCancelled, JobKind
from botai.core.vault import SecretKind
from botai.db.models import ChatMessage, ChatThread, Connection, Job, JobAnswer, Space, University
from botai.jobs import dialogue
from botai.jobs import queue as queue_module
from botai.jobs.context import PgJobContext
from botai.jobs.events import append_event, purge_finished
from botai.jobs.notify import Notifier
from botai.learn.intent import LearningIntent
from botai.spaces import conversations as source_conversations
from botai.spaces import service as source_service
from botai.spaces.conversations import bind_bootstrap, continue_source
from tests.jobs.env import Env

pytestmark = pytest.mark.db
URL = "https://schedule.fakeuni.test/week?begin_date=2026-10-26#/calendar"
INTENT: dict[str, Any] = {
    "entry_urls": [URL],
    "requested_kinds": ["scheduleEvents"],
    "absent_kinds": ["deadlines"],
    "user_note": "Дедлайнов здесь нет",
}
SECRET = "fixture-do-not-copy-Passphrase"


async def start(
    env: Env, *, context: dict[str, Any] | None = None
) -> tuple[httpx.AsyncClient, str, str, str]:
    client = env.new_client()
    account = await env.account(client)
    body: dict[str, Any] = {"purpose": "connect"}
    if context is not None:
        body["connectContext"] = context
    response = await client.post("/api/v1/chat/threads", json=body)
    assert response.status_code == 201, response.text
    thread = response.json()["id"]
    runtime = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    return client, account.id, thread, runtime["activeJob"]["id"]


def ctx(env: Env, user: str, job_id: str, connection: str | None = None) -> PgJobContext:
    return PgJobContext(
        job_id=job_id,
        user_id=user,
        connection_id=connection,
        db=env.sm,
        vault=env.services.vault,
        clock=env.clock,
        notifier=Notifier(None),
        poll_seconds=0.01,
        ask_ttl=timedelta(seconds=2),
    )


async def wait_pending(client: httpx.AsyncClient, thread: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            runtime = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
            if runtime["pending"]:
                return runtime
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("requested", [None, ["courses"], ["scheduleEvents"]])
async def test_general_source_discovers_types_while_explicit_kinds_stay_requested(
    env: Env, requested: list[str] | None
) -> None:
    context: dict[str, Any] = {"entryUrls": [URL]}
    if requested is not None:
        context["requestedKinds"] = requested
    _, _, thread_id, job_id = await start(env, context=context)
    async with env.sm() as session:
        thread = await session.get(ChatThread, thread_id)
        job = await session.get(Job, job_id)
        assert thread is not None
        assert job is not None
        mode = "auto" if requested is None else "requested"
        assert thread.connect_context.get("discovery_mode", "requested") == mode
        assert job.payload["learning_intent"].get("discovery_mode", "requested") == mode
        if requested is not None:
            assert thread.connect_context["requested_kinds"] == requested


async def test_explicit_clarification_clears_durable_automatic_discovery(env: Env) -> None:
    _, user_id, thread_id, job_id = await start(env, context={"entryUrls": [URL]})
    await ctx(env, user_id, job_id).checkpoint_learning(
        {
            "entry_urls": [URL],
            "requested_kinds": ["courses"],
            "absent_kinds": [],
            "user_note": "Только курсы",
        }
    )
    async with env.sm() as session:
        thread = await session.get(ChatThread, thread_id)
        job = await session.get(Job, job_id)
        assert thread is not None
        assert job is not None
        assert thread.connect_context.get("discovery_mode", "requested") == "requested"
        assert job.payload["learning_intent"].get("discovery_mode", "requested") == "requested"
        assert thread.connect_context["requested_kinds"] == ["courses"]


async def test_connect_is_atomic_idempotent_and_tenant_scoped(env: Env) -> None:
    client = env.new_client()
    account = await env.account(client)
    request_id = new_id()
    body = {
        "purpose": "connect",
        "clientRequestId": request_id,
        "connectContext": {"entryUrls": [URL], "absentKinds": ["deadlines"]},
    }
    one, two = await asyncio.gather(
        *(client.post("/api/v1/chat/threads", json=body) for _ in range(2))
    )
    assert one.status_code == two.status_code == 201
    assert one.json() == two.json()
    thread = one.json()["id"]
    async with env.sm() as session:
        row = await session.get(ChatThread, thread)
        assert row
        assert row.connect_context["entry_urls"] == [URL]
        assert (
            await session.scalar(
                select(func.count()).select_from(Job).where(Job.thread_id == thread)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(ChatMessage).where(ChatMessage.thread_id == thread)
            )
            == 1
        )
    conflict = await client.post("/api/v1/chat/threads", json={**body, "title": "Другой"})
    assert conflict.status_code == 409
    other = env.new_client()
    await env.account(other)
    assert (await other.get(f"/api/v1/chat/threads/{thread}/runtime")).status_code == 404
    assert (
        await other.post(f"/api/v1/chat/threads/{thread}/messages", json={"text": "x"})
    ).status_code == 404
    assert account.id


async def test_freeform_reuses_connect_and_checkpoint_is_replay_safe(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    worker = ctx(env, user, job_id)
    question = InputBlock(
        id=new_id(),
        at=env.clock.now(),
        question="Что подключаем?",
        required=True,
        input_kind="text",
        max_length=2000,
    )
    waiting = asyncio.create_task(worker.ask_dialogue(question))
    state = await wait_pending(client, thread)
    assert state["pending"] == [
        {"jobId": job_id, "blockId": question.id, "channel": "dialogue", "secret": False}
    ]
    message_id = new_id()
    body = {
        "text": f"Ссылка уже правильная {URL}, дедлайнов здесь нет",
        "targetJobId": job_id,
        "clientMessageId": message_id,
    }
    one, two = await asyncio.gather(
        *(client.post(f"/api/v1/chat/threads/{thread}/messages", json=body) for _ in range(2))
    )
    assert one.status_code == two.status_code == 202
    assert one.json() == two.json()
    assert one.json()["jobId"] == job_id
    saved_text = one.json()["message"]["blocks"][0]["markdown"]
    assert await waiting == saved_text
    recovered = ctx(env, user, job_id)
    replay = await recovered.drain_dialogue()
    assert [turn.text for turn in replay] == [saved_text]
    await recovered.checkpoint_learning(INTENT)
    assert await recovered.drain_dialogue() == []
    runtime = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    assert runtime["pending"] == []
    assert any(
        r["messageId"] == one.json()["message"]["id"] and r["status"] == "applied"
        for r in runtime["receipts"]
    )
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Job).where(Job.thread_id == thread)
            )
            == 1
        )
        saved = await session.get(ChatThread, thread)
        assert saved
        assert saved.connect_context["absent_kinds"] == ["deadlines"]
    assert (
        await client.post(
            f"/api/v1/jobs/{job_id}/answers", json={"blockId": question.id, "values": "late"}
        )
    ).status_code == 409
    assert (
        await client.post(
            f"/api/v1/chat/threads/{thread}/messages", json={**body, "text": "changed"}
        )
    ).status_code == 409


async def test_native_secret_never_enters_composer_history_or_receipts(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    worker = ctx(env, user, job_id)
    question = InputBlock(
        id=new_id(),
        at=env.clock.now(),
        question="Пароль",
        required=True,
        input_kind="password",
        secret=True,
        autocomplete="current-password",
    )
    waiting = asyncio.create_task(worker.ask_secret(question))
    await wait_pending(client, thread)
    response = await client.post(
        f"/api/v1/chat/threads/{thread}/messages", json={"text": SECRET, "targetJobId": job_id}
    )
    assert response.status_code == 409
    answer = await client.post(
        f"/api/v1/jobs/{job_id}/answers", json={"blockId": question.id, "values": SECRET}
    )
    assert answer.status_code == 204
    assert (await waiting).get_secret_value() == SECRET
    assert not await env.services.vault.has(user, question.id, SecretKind.TRANSIENT)
    history = await client.get(f"/api/v1/chat/threads/{thread}/messages")
    runtime = await client.get(f"/api/v1/chat/threads/{thread}/runtime")
    assert SECRET not in history.text + runtime.text
    rows = history.json()
    acknowledgement = [
        message
        for message in rows
        if message["role"] == "user" and message["blocks"][0].get("markdown") == "Пароль передан"
    ]
    assert len(acknowledgement) == 1
    assert rows.index(acknowledgement[0]) > next(
        index for index, message in enumerate(rows) if message["blocks"][0]["id"] == question.id
    )
    repeated = await client.post(
        f"/api/v1/jobs/{job_id}/answers", json={"blockId": question.id, "values": SECRET}
    )
    assert repeated.status_code == 409
    repeated_history = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    assert [message["id"] for message in repeated_history] == [message["id"] for message in rows]
    assert await worker.drain_dialogue() == []  # The safe marker is not a credential/LLM turn.
    async with env.sm() as session:
        job = await session.get(Job, job_id)
        assert job
        assert SECRET not in json.dumps(job.payload)
        answer_row = await session.scalar(select(JobAnswer).where(JobAnswer.job_id == job_id))
        assert answer_row
        assert answer_row.values is None
        assert answer_row.is_secret


async def test_history_block_identity_chronology_and_login_ticket_privacy(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    worker = ctx(env, user, job_id)
    block_id = new_id()
    await worker.emit(ThinkingBlock(id=block_id, at=env.clock.now(), stage="Смотрю сайт"))
    before = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    first = next(m for m in before if m["blocks"][0]["id"] == block_id)
    await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={"text": "Дедлайнов нет", "targetJobId": job_id},
    )
    await worker.emit(ThinkingBlock(id=block_id, at=env.clock.now(), stage="Проверяю расписание"))
    later = TextBlock(id=new_id(), at=env.clock.now(), markdown="Понял уточнение")
    await worker.emit(later)
    _, connection = await env.connection(user)
    login = LoginBlock(
        id=new_id(),
        at=env.clock.now(),
        connection_id=connection,
        ws_url="wss://screen.invalid/secret-ticket",
        ticket="fixture-ticket-do-not-store",
        expires_at=env.clock.now() + timedelta(minutes=5),
        hint="Продолжите вход",
    )
    await worker.emit(login)
    history = await client.get(f"/api/v1/chat/threads/{thread}/messages")
    rows = history.json()
    current = next(m for m in rows if m["blocks"][0]["id"] == block_id)
    assert (current["id"], current["createdAt"]) == (first["id"], first["createdAt"])
    assert current["blocks"][0]["stage"] == "Проверяю расписание"
    assert (
        rows.index(current)
        < next(
            i
            for i, m in enumerate(rows)
            if m["role"] == "user" and m["blocks"][0]["markdown"] == "Дедлайнов нет"
        )
        < next(i for i, m in enumerate(rows) if m["blocks"][0]["id"] == later.id)
    )
    assert "fixture-ticket-do-not-store" not in history.text
    assert "secret-ticket" not in history.text
    assert next(m for m in rows if m["blocks"][0]["id"] == login.id)["blocks"][0]["type"] == "text"


async def test_bootstrap_url_preserves_query_note_and_never_guesses_from_name(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    worker = ctx(env, user, job_id)
    boot = asyncio.create_task(bind_bootstrap(worker, env.services, {}))
    first = await wait_pending(client, thread)
    response = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={"text": "Тестовый университет", "targetJobId": job_id},
    )
    assert response.status_code == 202
    async with asyncio.timeout(3):
        while True:
            runtime = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
            if (
                runtime["pending"]
                and runtime["pending"][0]["blockId"] != first["pending"][0]["blockId"]
            ):
                break
            await asyncio.sleep(0.01)
    assert not boot.done()
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Connection).where(Connection.user_id == user)
            )
            == 0
        )
    response = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={"text": f"Вот {URL}, дедлайнов тут нет", "targetJobId": job_id},
    )
    assert response.status_code == 202
    connection, payload = await boot
    assert payload["url"] == URL
    assert payload["learning_intent"]["entry_urls"] == [URL]
    assert "дедлайнов тут нет" in payload["learning_intent"]["user_note"]
    assert worker.connection_id == connection
    async with env.sm() as session:
        saved = await session.get(Connection, connection)
        assert saved
        assert saved.origin == "https://schedule.fakeuni.test"
        assert saved.allow_hosts == ["schedule.fakeuni.test"]
    assert (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()["connection"][
        "id"
    ] == connection


@pytest.mark.parametrize(
    "bad",
    [
        "https://fakeuni.test/?access_token=" + SECRET,
        "fakeuni.test/?access_token=" + SECRET,
        "fakeuni.test/#/callback?code=" + SECRET,
        "https://user:" + SECRET + "@fakeuni.test/",
        "https://fakeuni.test/?token=" + SECRET + "x" * 1100,
    ],
)
async def test_reject_secret_urls_before_durable_state(env: Env, bad: str) -> None:
    client, user, thread, job_id = await start(env)
    for body in ({"purpose": "connect", "connectContext": {"entryUrls": [bad]}},):
        response = await client.post("/api/v1/chat/threads", json=body)
        assert response.status_code == 422
        assert SECRET not in response.text
    response = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={"text": "a" * 4500 + " " + bad, "targetJobId": job_id},
    )
    assert response.status_code == 422
    assert SECRET not in response.text
    space, _ = await env.connection(user)
    response = await client.post(f"/api/v1/spaces/{space}/connections", json={"url": bad})
    assert response.status_code == 422
    assert SECRET not in response.text
    async with env.sm() as session:
        messages = await session.scalars(select(ChatMessage).where(ChatMessage.user_id == user))
        jobs = await session.scalars(select(Job).where(Job.user_id == user))
        assert SECRET not in json.dumps([m.blocks for m in messages])
        assert SECRET not in json.dumps([j.payload for j in jobs])


async def test_validation_problem_never_echoes_secret_input_or_json_body(env: Env) -> None:
    client, user, _, _ = await start(env)
    _, connection = await env.connection(user)
    cases = [
        await client.put(
            f"/api/v1/connections/{connection}/secrets", json={"kind": "token", "value": [SECRET]}
        ),
        await client.post("/api/v1/chat/threads", content='{"password":"' + SECRET + '",broken'),
        await client.post("/api/v1/chat/threads", json={"spaceId": [SECRET]}),
    ]
    for response in cases:
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "validation"
        assert SECRET not in response.text
        assert "input" not in response.json()


async def test_terminal_history_survives_event_purge_and_chat_can_continue(env: Env) -> None:
    client, _user, thread, job_id = await start(env)
    worker = env.worker({})
    claimed = await worker.claim()
    assert claimed
    assert claimed.id == job_id
    await worker._finish(claimed, "done", result={"status": "error", "detail": "not_found"})
    env.advance(minutes=11)
    async with env.sm() as session:
        await purge_finished(session, env.clock.now())
        await session.commit()
    state = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    assert state["activeJob"]["id"] == job_id
    assert state["pending"] == []
    history = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    assert any("не завершено" in m["blocks"][0].get("markdown", "") for m in history)
    assert (
        await client.post(
            f"/api/v1/chat/threads/{thread}/messages", json={"text": "late", "targetJobId": job_id}
        )
    ).status_code == 409
    normal = await client.post(
        f"/api/v1/chat/threads/{thread}/messages", json={"text": "Что делать дальше?"}
    )
    assert normal.status_code == 202
    state = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    assert state["activeJob"]["kind"] == "chat"
    other = env.new_client()
    await env.account(other)
    assert (await other.get(f"/api/v1/jobs/{normal.json()['jobId']}")).status_code == 404


async def test_legacy_attach_sync_priority_and_unconfigured_repair_use_same_thread(
    env: Env,
) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    _, connection = await env.connection(user, status="error")
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=user, connection_id=connection
    )
    body = {"jobId": job_id}
    response = await client.post(f"/api/v1/connections/{connection}/conversation", json=body)
    assert response.status_code == 200
    thread = response.json()["id"]
    assert (await client.post(f"/api/v1/connections/{connection}/conversation", json=body)).json()[
        "id"
    ] == thread
    assert (await env.job(job_id)).thread_id == thread
    repair = await client.post(f"/api/v1/connections/{connection}/repair")
    assert repair.status_code == 202
    assert repair.json()["jobId"] == job_id
    async with env.sm() as session:
        job = await session.get(Job, job_id)
        assert job
        job.state = "done"
        conn = await session.get(Connection, connection)
        assert conn
        conn.status = "active"
        conn.adapter = "recipe"
        await session.commit()
    sync = await client.post(f"/api/v1/connections/{connection}/sync")
    assert sync.status_code == 202
    linked = await client.post(
        f"/api/v1/connections/{connection}/conversation", json={"jobId": sync.json()["jobId"]}
    )
    assert linked.json()["id"] == thread
    state = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    assert state["activeJob"]["kind"] == "sync"
    other = env.new_client()
    await env.account(other)
    assert (
        await other.post(f"/api/v1/connections/{connection}/conversation", json=body)
    ).status_code == 404


async def test_cross_host_continuation_is_same_chat_but_separate_connection_and_secrets(
    env: Env,
) -> None:
    client, user, thread, parent_job = await start(env, context={"entryUrls": [URL]})
    parent, _ = await bind_bootstrap(ctx(env, user, parent_job), env.services, {})
    await env.services.vault.put(user, parent, SecretKind.PASSWORD, SECRET)
    before_handoff = {
        "text": "Расписание нужно за эту неделю",
        "targetJobId": parent_job,
        "clientMessageId": new_id(),
    }
    accepted = await client.post(f"/api/v1/chat/threads/{thread}/messages", json=before_handoff)
    assert accepted.status_code == 202
    result = await continue_source(
        ctx(env, user, parent_job, parent),
        env.services,
        parent,
        "https://study.fakeuni.test/courses?week=4",
        INTENT,
    )
    child = await env.job(result["jobId"])
    assert child.thread_id == thread
    assert child.connection_id == result["connectionId"]
    assert child.payload["url"] == "https://study.fakeuni.test/courses?week=4"
    assert not await env.services.vault.has(user, result["connectionId"], SecretKind.PASSWORD)
    async with env.sm() as session:
        origin = await session.get(Connection, parent)
        target = await session.get(Connection, result["connectionId"])
        assert origin
        assert target
        assert origin.space_id == target.space_id
        assert origin.origin == "https://schedule.fakeuni.test"
        assert origin.allow_hosts == ["schedule.fakeuni.test"]
    state = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    assert state["activeJob"]["id"] == child.id
    assert state["connection"]["id"] == result["connectionId"]
    stale = {
        "text": "Новое уточнение старому заданию",
        "targetJobId": parent_job,
        "clientMessageId": new_id(),
    }
    assert (
        await client.post(f"/api/v1/chat/threads/{thread}/messages", json=stale)
    ).status_code == 409
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ChatMessage)
                .where(
                    ChatMessage.thread_id == thread,
                    ChatMessage.client_message_id == stale["clientMessageId"],
                )
            )
            == 0
        )
    current = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={
            "text": "Продолжаю по новой ссылке",
            "targetJobId": child.id,
            "clientMessageId": new_id(),
        },
    )
    assert current.status_code == 202
    replay = await client.post(f"/api/v1/chat/threads/{thread}/messages", json=before_handoff)
    assert replay.status_code == 202
    assert replay.json() == accepted.json()


async def test_cancel_rejects_late_dialogue_and_closes_pending(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    waiting = asyncio.create_task(
        ctx(env, user, job_id).ask_dialogue(
            InputBlock(
                id=new_id(),
                at=env.clock.now(),
                question="Ссылка?",
                required=True,
                input_kind="text",
            )
        )
    )
    await wait_pending(client, thread)
    assert (await client.post(f"/api/v1/jobs/{job_id}/cancel")).status_code == 204
    with pytest.raises(JobCancelled):
        await waiting
    state = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    assert state["pending"] == []
    assert state["activeJob"]["state"] == "cancelled"


async def test_chat_repair_resumes_unconfigured_connect_with_durable_followup(env: Env) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    _, connection = await env.connection(user, status="error")
    response = await client.post("/api/v1/chat/threads", json={})
    thread = response.json()["id"]
    tools = ChatEnv(
        services=env.services, user_id=user, thread_id=thread, job_id=None, space_id=None
    )
    result = await request_repair(
        tools, {"connection_id": connection, "reason": "Данные ещё не появились"}
    )
    assert "Подключение" in result
    state = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    assert state["activeJob"]["kind"] == "connect"
    job_id = state["activeJob"]["id"]
    assert (await env.job(job_id)).thread_id == thread
    assert "уже идёт" in await request_repair(tools, {"connection_id": connection})
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Job).where(Job.connection_id == connection)
            )
            == 1
        )
    worker = env.worker({})
    claimed = await worker.claim()
    assert claimed is not None
    await worker._fail(claimed, "login_failed", "Нужно продолжить вход", 400)
    history = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    assert any("Нужно продолжить вход" in m["blocks"][0].get("markdown", "") for m in history)


@pytest.mark.parametrize("status", ["error", "needs_reauth"])
@pytest.mark.parametrize("requested", [None, ["scheduleEvents"]])
async def test_manual_login_resumes_canonical_connect_and_preserves_intent(
    env: Env, status: str, requested: list[str] | None
) -> None:
    context: dict[str, Any] = {"entryUrls": [URL], "absentKinds": ["deadlines"]}
    if requested is not None:
        context["requestedKinds"] = requested
    client, user, thread, old_id = await start(env, context=context)
    connection, _ = await bind_bootstrap(ctx(env, user, old_id), env.services, {})
    async with env.sm() as session:
        old = await session.get(Job, old_id)
        conn = await session.get(Connection, connection)
        assert old is not None
        assert conn is not None
        old.state = "done"
        conn.status = status
        await session.commit()
    other_thread = (await client.post("/api/v1/chat/threads", json={})).json()["id"]
    tools = ChatEnv(
        services=env.services, user_id=user, thread_id=other_thread, job_id=None, space_id=None
    )
    results = await asyncio.gather(
        *(request_login(tools, {"connection_id": connection}) for _ in range(2))
    )
    runtime = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    new_id = runtime["activeJob"]["id"]
    assert new_id != old_id
    resumed = await env.job(new_id)
    assert resumed.kind == "connect"
    assert resumed.thread_id == thread
    assert resumed.payload["url"] == URL
    assert resumed.payload["manual_login"] is True
    assert resumed.payload["source"] == "chat"
    intent = LearningIntent.restore(resumed.payload["learning_intent"])
    assert intent.discovery_mode == ("auto" if requested is None else "requested")
    assert intent.entry_urls == (URL,)
    assert intent.absent_kinds == ("deadlines",)
    assert all(new_id in result for result in results)
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Job).where(Job.connection_id == connection)
            )
            == 2
        )


@pytest.mark.parametrize(
    ("kind", "state"),
    [("connect", "waiting_user"), ("learn", "running"), ("repair", "queued"), ("sync", "running")],
)
async def test_manual_login_returns_active_source_without_replacing_it(
    env: Env, kind: str, state: str
) -> None:
    client, user, thread, old_id = await start(env, context={"entryUrls": [URL]})
    connection, _ = await bind_bootstrap(ctx(env, user, old_id), env.services, {})
    active_id = new_id()
    active_payload: dict[str, Any] = {"existing": True}
    async with env.sm() as session:
        old = await session.get(Job, old_id)
        assert old is not None
        old.state = "done"
        active_job = Job(
            id=active_id,
            user_id=user,
            connection_id=connection,
            thread_id=thread,
            kind=kind,
            state=state,
            payload=active_payload,
            run_after=env.clock.now(),
        )
        if kind == "connect":
            dialogue.set_pending(
                active_job,
                InputBlock(
                    id=new_id(),
                    at=env.clock.now(),
                    question="Пароль",
                    required=True,
                    input_kind="password",
                    secret=True,
                ),
                channel="native",
                now=env.clock.now(),
            )
            active_payload = dict(active_job.payload)
        session.add(active_job)
        await session.commit()
    tools = ChatEnv(
        services=env.services, user_id=user, thread_id=thread, job_id=None, space_id=None
    )
    result = await request_login(tools, {"connection_id": connection})
    assert active_id in result
    assert "уже идёт" in result
    assert "Новый вход не запущен" in result
    assert thread in result
    active = await env.job(active_id)
    assert active.kind == kind
    assert active.state == state
    assert active.payload == active_payload
    if kind == "connect":
        assert "защищённые поля" in result
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Job).where(Job.connection_id == connection)
            )
            == 2
        )
    runtime = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    assert runtime["activeJob"]["id"] == active_id


async def test_manual_login_rejects_foreign_missing_and_disabled_connections(env: Env) -> None:
    client, user, thread, _ = await start(env)
    other = (await env.account()).id
    _, foreign = await env.connection(other, status="needs_reauth")
    _, disabled = await env.connection(user, status="disabled")
    tools = ChatEnv(
        services=env.services, user_id=user, thread_id=thread, job_id=None, space_id=None
    )
    assert "Такого подключения" in await request_login(tools, {"connection_id": foreign})
    assert "Такого подключения" in await request_login(tools, {"connection_id": new_id()})
    assert "отключено" in await request_login(tools, {"connection_id": disabled})
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.connection_id.in_([foreign, disabled]))
            )
            == 0
        )
    assert (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).status_code == 200


@pytest.mark.parametrize("competing_kind", [JobKind.CONNECT, JobKind.SYNC])
async def test_continuation_waits_for_source_group_before_locking_existing_child(
    env: Env, monkeypatch: pytest.MonkeyPatch, competing_kind: JobKind
) -> None:
    _client, user, thread, parent_job = await start(env, context={"entryUrls": [URL]})
    parent, _ = await bind_bootstrap(ctx(env, user, parent_job), env.services, {})
    child_url = "https://child.fakeuni.test/courses?week=4"
    child_id = new_id()
    async with env.sm() as session:
        parent_row = await session.get(Connection, parent)
        assert parent_row is not None
        session.add(
            Connection(
                id=child_id,
                user_id=user,
                space_id=parent_row.space_id,
                origin="https://child.fakeuni.test",
                title="Child",
                kind="other",
                status="draft",
            )
        )
        await session.commit()
    real_lock = queue_module.lock_sources
    requesting = asyncio.Event()
    async with env.sm() as producer:
        await real_lock(producer, [child_id])

        async def observed_lock(session: AsyncSession, ids: Iterable[str]) -> None:
            ids = tuple(ids)
            if session is not producer and child_id in ids:
                requesting.set()
            await real_lock(session, ids)

        monkeypatch.setattr(queue_module, "lock_sources", observed_lock)
        monkeypatch.setattr(source_conversations, "lock_sources", observed_lock)
        continuation = asyncio.create_task(
            continue_source(
                ctx(env, user, parent_job, parent), env.services, parent, child_url, INTENT
            )
        )
        try:
            await asyncio.wait_for(requesting.wait(), 2)
            async with env.sm() as observer:
                # NOWAIT is the assertion: waiting for a source group must not already
                # hold the child row that a queued job's FK needs in KEY SHARE mode.
                row = await observer.scalar(
                    select(Connection).where(Connection.id == child_id).with_for_update(nowait=True)
                )
                assert row is not None
                await observer.commit()
            queued = await queue_module.enqueue_in_session(
                producer,
                competing_kind,
                user_id=user,
                connection_id=child_id,
                thread_id=thread,
                payload={"manual_login": True} if competing_kind == JobKind.CONNECT else {},
                now=env.clock.now(),
            )
            await producer.commit()
            result = await asyncio.wait_for(continuation, 2)
        finally:
            if not continuation.done():
                continuation.cancel()
                await asyncio.gather(continuation, return_exceptions=True)
    assert result["connectionId"] == child_id
    if competing_kind == JobKind.CONNECT:
        assert result["jobId"] == queued.id
    continued = await env.job(result["jobId"])
    assert continued.thread_id == thread
    assert (await env.job(parent_job)).payload["continuation_job_id"] == continued.id


async def test_manual_login_defers_legacy_thread_binding_until_existing_job_is_unlocked(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    _, connection = await env.connection(user, status="active")
    thread_id = (await client.post("/api/v1/chat/threads", json={})).json()["id"]
    old_id = await env.services.queue.enqueue(
        JobKind.SYNC, user_id=user, connection_id=connection, thread_id=thread_id
    )
    real_lock = queue_module.lock_sources
    requesting = asyncio.Event()

    async def observed_lock(session: AsyncSession, ids: Iterable[str]) -> None:
        requesting.set()
        await real_lock(session, ids)

    monkeypatch.setattr(queue_module, "lock_sources", observed_lock)
    async with env.sm() as finishing:
        old = await finishing.scalar(select(Job).where(Job.id == old_id).with_for_update())
        assert old is not None

        async def login() -> str:
            async with env.sm() as session:
                return await source_service.enqueue_for_connection(
                    session,
                    env.services.queue,
                    user,
                    connection,
                    JobKind.CONNECT,
                    thread_id=thread_id,
                    payload={"manual_login": True},
                    now=env.clock.now(),
                )

        pending = asyncio.create_task(login())
        try:
            await asyncio.wait_for(requesting.wait(), 2)
            async with env.sm() as observer:
                row = await observer.scalar(
                    select(ChatThread)
                    .where(ChatThread.id == thread_id)
                    .with_for_update(nowait=True)
                )
                assert row is not None
                await observer.commit()
            # Match worker terminal projection's Job -> Thread order.
            await finishing.scalar(
                select(ChatThread).where(ChatThread.id == thread_id).with_for_update()
            )
            old.state = "done"
            await finishing.commit()
            resumed_id = await asyncio.wait_for(pending, 2)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert resumed_id != old_id
    resumed = await env.job(resumed_id)
    assert resumed.payload["manual_login"] is True
    assert resumed.thread_id == thread_id


async def test_attach_replays_safe_legacy_updates_and_rejects_mismatch_disabled(env: Env) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    _, connection = await env.connection(user)
    _, foreign_connection = await env.connection(user)
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=user, connection_id=connection
    )
    block = ThinkingBlock(id=new_id(), at=env.clock.now(), stage="Проверяю сайт")
    async with env.sm() as session:
        await append_event(
            session,
            job_id,
            "block",
            block.model_dump(by_alias=True, mode="json", exclude_none=True),
        )
        await append_event(
            session, job_id, "block_update", {"id": block.id, "patch": {"state": "ok"}}
        )
        await session.commit()
    response = await client.post(
        f"/api/v1/connections/{connection}/conversation", json={"jobId": job_id}
    )
    assert response.status_code == 200
    thread = response.json()["id"]
    history = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    assert len(history) == 1
    assert history[0]["blocks"][0]["state"] == "ok"
    assert (
        await client.post(
            f"/api/v1/connections/{foreign_connection}/conversation", json={"jobId": job_id}
        )
    ).status_code == 404
    async with env.sm() as session:
        conn = await session.get(Connection, connection)
        assert conn is not None
        conn.status = "disabled"
        await session.commit()
    assert (
        await client.post(f"/api/v1/connections/{connection}/conversation", json={"jobId": job_id})
    ).status_code == 403
    assert (
        await client.post(
            f"/api/v1/chat/threads/{thread}/messages", json={"text": "hi", "targetJobId": job_id}
        )
    ).status_code == 403


async def test_legacy_cross_host_continuation_creates_canonical_thread_before_ui_attach(
    env: Env,
) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    _, parent = await env.connection(user, status="draft", origin="https://schedule.fakeuni.test")
    parent_job = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=user, connection_id=parent
    )
    result = await continue_source(
        ctx(env, user, parent_job, parent),
        env.services,
        parent,
        "https://study.fakeuni.test/courses?week=4",
        INTENT,
    )
    old = await env.job(parent_job)
    child = await env.job(result["jobId"])
    assert old.thread_id is not None
    assert child.thread_id == old.thread_id
    attached = await client.post(
        f"/api/v1/connections/{parent}/conversation", json={"jobId": parent_job}
    )
    assert attached.status_code == 200
    assert attached.json()["id"] == old.thread_id
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ChatThread).where(ChatThread.user_id == user)
            )
            == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(Job).where(Job.user_id == user))
            == 2
        )


async def test_concurrent_parent_continuations_deduplicate_child_without_lock_inversion(
    env: Env,
) -> None:
    _, user, thread, first_job = await start(env, context={"entryUrls": [URL]})
    first, _ = await bind_bootstrap(ctx(env, user, first_job), env.services, {})
    async with env.sm() as session:
        parent = await session.get(Connection, first)
        assert parent is not None
        second = Connection(
            id=new_id(),
            user_id=user,
            space_id=parent.space_id,
            origin="https://other.fakeuni.test",
            kind="other",
            title="Другой источник",
            status="draft",
            allow_hosts=["other.fakeuni.test"],
        )
        session.add(second)
        await session.commit()
    second_job = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=user, connection_id=second.id, thread_id=thread
    )
    url = "https://study.fakeuni.test/courses?week=4"
    async with asyncio.timeout(5):
        one, two = await asyncio.gather(
            continue_source(ctx(env, user, first_job, first), env.services, first, url, INTENT),
            continue_source(
                ctx(env, user, second_job, second.id), env.services, second.id, url, INTENT
            ),
        )
    assert one == two
    child = await env.job(one["jobId"])
    assert child.thread_id == thread
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Connection)
                .where(
                    Connection.user_id == user, Connection.origin == "https://study.fakeuni.test"
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.user_id == user, Job.connection_id == one["connectionId"])
            )
            == 1
        )


@pytest.mark.parametrize("safe_warning", [True, False])
async def test_known_university_warning_is_safe_durable_once_before_native_login(
    env: Env, safe_warning: bool
) -> None:
    warning = "Сайт просит не использовать сторонние приложения."
    university_id = "warning-fixture-" + new_id().lower()
    async with env.sm() as session:
        session.add(
            University(
                id=university_id,
                title="Фикстурный вуз",
                short_title="ФУ",
                origins=["https://schedule.fakeuni.test"],
                accent_hue=280,
                third_party_warning=warning
                if safe_warning
                else "Перейдите на https://untrusted.fakeuni.test/",
            )
        )
        await session.commit()
    client, user, thread, job_id = await start(
        env, context={"universityId": university_id, "entryUrls": [URL]}
    )
    worker = ctx(env, user, job_id)
    connection, _ = await bind_bootstrap(worker, env.services, {})
    await bind_bootstrap(worker, env.services, {})  # Recovery of the same job cannot duplicate it.
    native = InputBlock(
        id=new_id(),
        at=env.clock.now(),
        question="Пароль",
        input_kind="password",
        required=True,
        secret=True,
        autocomplete="current-password",
    )
    await worker.emit(native)
    events = await env.events(job_id)
    warnings = [
        event
        for event in events
        if event.type == "block"
        and event.data.get("type") == "text"
        and warning in event.data.get("markdown", "")
    ]
    assert len(warnings) == (1 if safe_warning else 0)
    if safe_warning:
        assert warnings[0].seq < next(
            event.seq for event in events if event.data.get("id") == native.id
        )
    history = await client.get(f"/api/v1/chat/threads/{thread}/messages")
    assert history.status_code == 200
    assert (warning in history.text) is safe_warning
    assert "untrusted.fakeuni.test" not in history.text
    assert (await env.job(job_id)).connection_id == connection


@pytest.mark.parametrize("prior_note", ["", "Нужно только расписание занятий"])
async def test_url_only_bootstrap_keeps_address_out_of_note_and_preserves_prior_intent(
    env: Env, prior_note: str
) -> None:
    client, user, thread, job_id = await start(env, context={"userNote": prior_note})
    worker = ctx(env, user, job_id)
    boot = asyncio.create_task(bind_bootstrap(worker, env.services, {}))
    await wait_pending(client, thread)
    response = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={"text": URL, "targetJobId": job_id, "clientMessageId": new_id()},
    )
    assert response.status_code == 202
    _, payload = await boot
    assert payload["learning_intent"]["entry_urls"] == [URL]
    assert payload["learning_intent"]["user_note"] == prior_note
    assert await worker.drain_dialogue() == []


@pytest.mark.parametrize("child_context", [False, True])
async def test_unconfigured_repair_restores_own_intent_url_query_and_current_job(
    env: Env, child_context: bool
) -> None:
    client, user, thread, first_job = await start(
        env,
        context={"entryUrls": [URL], "absentKinds": ["deadlines"], "userNote": INTENT["user_note"]},
    )
    parent, _ = await bind_bootstrap(ctx(env, user, first_job), env.services, {})
    await env.services.vault.put(user, parent, SecretKind.PASSWORD, SECRET)
    async with env.sm() as session:
        old = await session.get(Job, first_job)
        conn = await session.get(Connection, parent)
        assert old is not None
        assert conn is not None
        old.state = "done"
        old.result = {"status": "error", "detail": "not_found"}
        old.finished_at = env.clock.now()
        conn.status = "error"
        if child_context:
            conversation = await session.get(ChatThread, thread)
            assert conversation is not None
            conversation.connect_context = {
                **conversation.connect_context,
                "entry_urls": ["https://child.fakeuni.test/courses"],
                "requested_kinds": ["grades"],
                "absent_kinds": ["scheduleEvents"],
                "user_note": "Оценки на другом сайте",
            }
        await session.commit()
    response = await client.post(f"/api/v1/connections/{parent}/repair")
    assert response.status_code == 202
    job_id = response.json()["jobId"]
    assert job_id != first_job
    retried = await env.job(job_id)
    assert retried.kind == "connect"
    assert retried.thread_id == thread
    assert retried.payload["url"] == URL
    assert retried.payload["learning_intent"] == {**INTENT, "discovery_mode": "auto"}
    assert retried.payload["kinds"] == ["scheduleEvents"]
    assert "child.fakeuni.test" not in json.dumps(retried.payload)
    assert SECRET not in json.dumps(retried.payload)
    assert await env.services.vault.has(user, parent, SecretKind.PASSWORD)
    runtime = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    assert runtime["activeJob"]["id"] == job_id
    stale = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={"text": "старый ответ", "targetJobId": first_job},
    )
    assert stale.status_code == 409
    current = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={"text": "Продолжаю с этой ссылки", "targetJobId": job_id},
    )
    assert current.status_code == 202
    assert current.json()["jobId"] == job_id


async def test_legacy_unbound_connect_retry_retains_both_kinds_and_own_url(env: Env) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    space = (await client.post("/api/v1/spaces", json={"title": "Фикстурный вуз"})).json()["id"]
    initial = await client.post(f"/api/v1/spaces/{space}/connections", json={"url": URL})
    assert initial.status_code == 201
    body = initial.json()
    conn_id, initial_job = body["connection"]["id"], body["jobId"]
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ChatThread).where(ChatThread.user_id == user)
            )
            == 0
        )
        job = await session.get(Job, initial_job)
        conn = await session.get(Connection, conn_id)
        assert job is not None
        assert conn is not None
        job.state = "failed"
        job.finished_at = env.clock.now()
        conn.status = "error"
        await session.commit()
    retry = await client.post(f"/api/v1/connections/{conn_id}/repair")
    assert retry.status_code == 202
    job = await env.job(retry.json()["jobId"])
    assert job.id != initial_job
    assert job.kind == "connect"
    assert job.thread_id is None
    assert job.payload["url"] == URL
    assert job.payload["kinds"] == ["scheduleEvents", "deadlines"]
    assert "learning_intent" not in job.payload


@pytest.mark.parametrize("explicit_space", [False, True])
async def test_known_university_adds_connection_to_existing_owned_space(
    env: Env, explicit_space: bool
) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    foreign_client = env.new_client()
    foreign = (await env.account(foreign_client)).id
    university_id = "known-fixture-" + new_id().lower()
    first_space, second_space, foreign_space = new_id(), new_id(), new_id()
    selected = second_space if explicit_space else first_space
    new_url = "https://new.fakeuni.test/schedule?begin_date=2026-10-26"
    async with env.sm() as session:
        session.add(
            University(
                id=university_id,
                title="Фикстурный вуз",
                short_title="ФУ",
                origins=["https://old.fakeuni.test"],
                accent_hue=280,
            )
        )
        await session.flush()
        session.add_all(
            [
                Space(
                    id=foreign_space,
                    user_id=foreign,
                    university_id=university_id,
                    title="Чужое",
                    created_at=env.clock.now() - timedelta(days=3),
                ),
                Space(
                    id=first_space,
                    user_id=user,
                    university_id=university_id,
                    title="Первое",
                    created_at=env.clock.now() - timedelta(days=2),
                ),
                Space(
                    id=second_space,
                    user_id=user,
                    university_id=university_id,
                    title="Явное",
                    created_at=env.clock.now() - timedelta(days=1),
                ),
            ]
        )
        await session.flush()
        session.add(
            Connection(
                id=new_id(),
                user_id=user,
                space_id=selected,
                origin="https://old.fakeuni.test",
                kind="portal",
                title="Первый источник",
                status="active",
                allow_hosts=["old.fakeuni.test"],
            )
        )
        await session.commit()
    body: dict[str, Any] = {
        "purpose": "connect",
        "connectContext": {"universityId": university_id, "entryUrls": [new_url]},
    }
    if explicit_space:
        body["spaceId"] = second_space
    response = await client.post("/api/v1/chat/threads", json=body)
    assert response.status_code == 201
    thread = response.json()["id"]
    runtime = (await client.get(f"/api/v1/chat/threads/{thread}/runtime")).json()
    job_id = runtime["activeJob"]["id"]
    worker = ctx(env, user, job_id)
    connection_id, _ = await bind_bootstrap(worker, env.services, {})
    repeated, _ = await bind_bootstrap(worker, env.services, {})
    assert repeated == connection_id
    async with env.sm() as session:
        row = await session.get(ChatThread, thread)
        conn = await session.get(Connection, connection_id)
        uni = await session.get(University, university_id)
        assert row is not None
        assert conn is not None
        assert uni is not None
        assert row.space_id == selected
        assert conn.space_id == selected
        assert conn.origin == "https://new.fakeuni.test"
        assert conn.allow_hosts == ["new.fakeuni.test"]
        assert uni.origins == ["https://old.fakeuni.test"]
        assert (
            await session.scalar(
                select(func.count()).select_from(Space).where(Space.user_id == user)
            )
            == 2
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Connection)
                .where(Connection.user_id == user, Connection.space_id == selected)
            )
            == 2
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Connection)
                .where(Connection.space_id == foreign_space)
            )
            == 0
        )


async def test_concurrent_known_university_bootstraps_create_one_owned_space(env: Env) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    other_client = env.new_client()
    other = (await env.account(other_client)).id
    university_id = "concurrent-fixture-" + new_id().lower()
    async with env.sm() as session:
        session.add(
            University(
                id=university_id,
                title="Фикстурный вуз",
                short_title="ФУ",
                origins=[],
                accent_hue=280,
            )
        )
        await session.flush()
        session.add(Space(id=new_id(), user_id=other, university_id=university_id, title="Чужое"))
        await session.commit()
    jobs: list[str] = []
    for host in ["one.fakeuni.test", "two.fakeuni.test"]:
        response = await client.post(
            "/api/v1/chat/threads",
            json={
                "purpose": "connect",
                "connectContext": {
                    "universityId": university_id,
                    "entryUrls": [f"https://{host}/schedule"],
                },
            },
        )
        assert response.status_code == 201
        runtime = (await client.get(f"/api/v1/chat/threads/{response.json()['id']}/runtime")).json()
        jobs.append(runtime["activeJob"]["id"])
    async with asyncio.timeout(5):
        results = await asyncio.gather(
            *(bind_bootstrap(ctx(env, user, job), env.services, {}) for job in jobs)
        )
    async with env.sm() as session:
        spaces = list(
            await session.scalars(
                select(Space).where(Space.user_id == user, Space.university_id == university_id)
            )
        )
        assert len(spaces) == 1
        connections = list(
            await session.scalars(
                select(Connection).where(
                    Connection.user_id == user, Connection.id.in_([result[0] for result in results])
                )
            )
        )
        assert len(connections) == 2
        assert {connection.space_id for connection in connections} == {spaces[0].id}
        assert {connection.origin for connection in connections} == {
            "https://one.fakeuni.test",
            "https://two.fakeuni.test",
        }
