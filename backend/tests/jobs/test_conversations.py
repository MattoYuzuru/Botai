"""Tasks inside threads (FR-004/007/016/022/030): routing, projection, continuation; real PG.

A thread has one agent. A task (connect, repair, sync, ingest) lives in the thread where it was
asked (`jobs.thread_id`); a message goes to the task only while it waits for a plain answer in
the dialogue, every other message starts an agent turn (`CHAT`). No live sites.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.env import ChatEnv, thread_link
from botai.agent.tools_sources import request_login, request_repair
from botai.contracts.agent_ui import (
    ChoiceBlock,
    ChoiceOption,
    InputBlock,
    LoginBlock,
    TextBlock,
    ThinkingBlock,
)
from botai.core.ids import new_id
from botai.core.jobs import JobCancelled, JobKind
from botai.core.vault import SecretKind
from botai.db.models import ChatMessage, ChatThread, Connection, Job, JobAnswer, Space
from botai.jobs import dialogue
from botai.jobs import queue as queue_module
from botai.jobs.context import PgJobContext
from botai.jobs.events import append_event, purge_finished
from botai.jobs.notify import Notifier
from botai.learn.intent import LearningIntent
from botai.spaces import conversations as source_conversations
from botai.spaces import service as source_service
from botai.spaces.conversations import continue_source, intent_of, normalize_context
from tests.jobs.env import Env

pytestmark = pytest.mark.db
URL = "https://schedule.fakeuni.test/week?begin_date=2026-10-26#/calendar"
ORIGIN = "https://schedule.fakeuni.test"
CHILD_URL = "https://study.fakeuni.test/courses?week=4"
INTENT: dict[str, Any] = {
    "entry_urls": [URL],
    "requested_kinds": ["scheduleEvents"],
    "absent_kinds": ["deadlines"],
    "user_note": "Дедлайнов здесь нет",
}
SECRET = "fixture-do-not-copy-Passphrase"
THREAD_OUT_KEYS = {
    "id",
    "spaceId",
    "title",
    "createdAt",
    "lastMessageAt",
    "unread",
    "attention",
    "activeTask",
    "preview",
}


async def student(env: Env) -> tuple[httpx.AsyncClient, str, str]:
    """A logged-in student and an empty thread: (client, user_id, thread_id)."""
    client = env.new_client()
    account = await env.account(client)
    response = await client.post("/api/v1/chat/threads", json={})
    assert response.status_code == 201, response.text
    return client, account.id, response.json()["id"]


async def start(
    env: Env, *, payload: dict[str, Any] | None = None
) -> tuple[httpx.AsyncClient, str, str, str]:
    """A student, a plain thread and a CONNECT task of that thread without a connection yet."""
    client, user, thread = await student(env)
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT,
        user_id=user,
        thread_id=thread,
        payload=payload if payload is not None else {"source": "chat"},
        dedupe=False,
    )
    return client, user, thread, job_id


def ctx(env: Env, user: str, job_id: str, connection: str | None = None) -> PgJobContext:
    return PgJobContext(
        job_id=job_id,
        user_id=user,
        connection_id=connection,
        db=env.sm,
        vault=env.services.vault,
        clock=env.clock,
        notifier=Notifier(None),
        allow_unclaimed=True,
        poll_seconds=0.01,
        ask_ttl=timedelta(seconds=2),
    )


async def source_task(
    env: Env,
    user: str,
    thread: str | None,
    *,
    context: dict[str, Any] | None = None,
    status: str = "draft",
    origin: str = ORIGIN,
    space: str | None = None,
) -> tuple[str, str]:
    """(connection, job): a source being set up in `thread`, as `connect_source` leaves it."""
    host = urlsplit(origin).hostname or ""
    async with env.sm() as session:
        if space is None:
            row = Space(id=new_id(), user_id=user, title="Фикстурный вуз")
            session.add(row)
            await session.flush()
            space = row.id
        conn = Connection(
            id=new_id(),
            user_id=user,
            space_id=space,
            origin=origin,
            kind="other",
            title="Расписание",
            status=status,
            allow_hosts=[host],
        )
        session.add(conn)
        await session.commit()
    raw = context if context is not None else {"entry_urls": [URL]}
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT,
        user_id=user,
        connection_id=conn.id,
        thread_id=thread,
        payload={
            "url": raw["entry_urls"][0] if raw.get("entry_urls") else URL,
            "learning_intent": intent_of(normalize_context(raw)),
            "source": "chat",
        },
    )
    return conn.id, job_id


async def runtime_of(client: httpx.AsyncClient, thread: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/chat/threads/{thread}/runtime")
    assert response.status_code == 200, response.text
    return response.json()


async def wait_pending(client: httpx.AsyncClient, thread: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            runtime = await runtime_of(client, thread)
            if runtime["pending"]:
                return runtime
            await asyncio.sleep(0.01)


async def post(client: httpx.AsyncClient, thread: str, text: str, **extra: str) -> httpx.Response:
    return await client.post(
        f"/api/v1/chat/threads/{thread}/messages", json={"text": text, **extra}
    )


async def chat_jobs(env: Env, thread: str) -> list[Job]:
    async with env.sm() as session:
        return list(
            await session.scalars(
                select(Job).where(Job.thread_id == thread, Job.kind == JobKind.CHAT.value)
            )
        )


async def finish(env: Env, job_id: str, *, result: dict[str, Any] | None = None) -> None:
    """End a task the way the runner does: terminal state plus its line in the thread."""
    async with env.sm() as session:
        job = await session.get(Job, job_id, with_for_update=True)
        assert job is not None
        job.state = "done"
        job.result = result
        job.finished_at = env.clock.now()
        await dialogue.project_terminal(session, job, env.clock.now())
        await session.commit()


def dialogue_question(env: Env, question: str = "Что подключаем?") -> InputBlock:
    return InputBlock(
        id=new_id(),
        at=env.clock.now(),
        question=question,
        required=True,
        input_kind="text",
        max_length=2000,
    )


def password_question(env: Env) -> InputBlock:
    return InputBlock(
        id=new_id(),
        at=env.clock.now(),
        question="Пароль",
        required=True,
        input_kind="password",
        secret=True,
        autocomplete="current-password",
    )


# --- threads ------------------------------------------------------------------------------------


async def test_thread_creation_is_idempotent_and_tenant_scoped(env: Env) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    request_id = new_id()
    body = {"clientRequestId": request_id, "title": "Расписание"}
    one, two = await asyncio.gather(
        *(client.post("/api/v1/chat/threads", json=body) for _ in range(2))
    )
    assert one.status_code == two.status_code == 201
    assert one.json() == two.json()
    created = one.json()
    assert set(created) == THREAD_OUT_KEYS
    assert created["title"] == "Расписание"
    assert created["activeTask"] is None
    assert created["attention"] is None
    thread = created["id"]
    same = await client.post("/api/v1/chat/threads", json={"clientRequestId": request_id})
    assert same.status_code == 201
    assert same.json()["id"] == thread
    space = (await client.post("/api/v1/spaces", json={"title": "Фикстурный вуз"})).json()["id"]
    for conflict in ({**body, "title": "Другой"}, {**body, "spaceId": space}):
        assert (await client.post("/api/v1/chat/threads", json=conflict)).status_code == 409
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ChatThread).where(ChatThread.user_id == user)
            )
            == 1
        )
        # A thread has no purpose: creating it starts no task and writes no message.
        assert (
            await session.scalar(
                select(func.count()).select_from(Job).where(Job.thread_id == thread)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(ChatMessage).where(ChatMessage.thread_id == thread)
            )
            == 0
        )
    other = env.new_client()
    await env.account(other)
    assert (await other.get(f"/api/v1/chat/threads/{thread}/runtime")).status_code == 404
    assert (await post(other, thread, "x")).status_code == 404


# --- routing of a message -----------------------------------------------------------------------


async def test_running_task_does_not_block_a_new_agent_turn(env: Env) -> None:
    client, _user, thread, job_id = await start(env)
    async with env.sm() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.state = "running"
        job.stage = "Смотрю сайт"
        await session.commit()
    response = await post(client, thread, "Что у меня завтра?")
    assert response.status_code == 202, response.text
    chat_id = response.json()["jobId"]
    assert chat_id != job_id
    chat = await env.job(chat_id)
    assert chat.kind == "chat"
    assert chat.thread_id == thread
    assert chat.payload == {"thread_id": thread, "message_id": response.json()["message"]["id"]}
    task = await env.job(job_id)
    assert task.state == "running"
    assert dialogue.state_of(task).get("inbox") in (None, [])
    assert dialogue.input_revision(task) == 0
    runtime = await runtime_of(client, thread)
    assert runtime["activeJob"]["id"] == job_id  # the task needs attention before the turn
    assert [t["id"] for t in runtime["tasks"]] == [job_id]
    assert runtime["pending"] == []


async def test_dialogue_question_receives_the_message_without_a_new_turn(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    question = dialogue_question(env)
    waiting = asyncio.create_task(ctx(env, user, job_id).ask_dialogue(question))
    state = await wait_pending(client, thread)
    assert state["pending"] == [
        {"jobId": job_id, "blockId": question.id, "channel": "dialogue", "secret": False}
    ]
    response = await post(client, thread, f"Вот ссылка {URL}")
    assert response.status_code == 202, response.text
    assert response.json()["jobId"] == job_id
    assert response.json()["message"]["jobId"] == job_id
    assert await waiting == f"Вот ссылка {URL}"
    assert await chat_jobs(env, thread) == []
    task = await env.job(job_id)
    inbox = dialogue.state_of(task)["inbox"]
    assert inbox == [
        {"message_id": response.json()["message"]["id"], "reply_to_block_id": question.id}
    ]


@pytest.mark.parametrize("asking", ["choice", "login"])
async def test_native_question_leaves_the_message_to_the_agent(env: Env, asking: str) -> None:
    client, user, thread, job_id = await start(env)
    worker = ctx(env, user, job_id)
    waiting: asyncio.Task[Any] | None = None
    if asking == "choice":
        block: ChoiceBlock | LoginBlock = ChoiceBlock(
            id=new_id(),
            at=env.clock.now(),
            question="Что нужно из источника?",
            mode="single",
            required=True,
            options=[
                ChoiceOption(value="a", label="Расписание"),
                ChoiceOption(value="b", label="Оценки"),
            ],
        )
        waiting = asyncio.create_task(worker.ask(block))
        channel = "native"
    else:
        _, connection = await env.connection(user)
        block = LoginBlock(
            id=new_id(),
            at=env.clock.now(),
            connection_id=connection,
            ws_url="wss://screen.invalid/fixture",
            ticket="fixture-ticket",
            expires_at=env.clock.now() + timedelta(minutes=5),
        )
        await worker.emit(block)
        channel = "manual"
    state = await wait_pending(client, thread)
    assert state["pending"] == [
        {"jobId": job_id, "blockId": block.id, "channel": channel, "secret": False}
    ]
    try:
        response = await post(client, thread, "А чем они отличаются?")
        assert response.status_code == 202, response.text
        assert response.json()["jobId"] != job_id
        assert (await env.job(response.json()["jobId"])).kind == "chat"
        task = await env.job(job_id)
        assert dialogue.state_of(task).get("inbox") in (None, [])
        assert (await runtime_of(client, thread))["pending"][0]["blockId"] == block.id
        if waiting is not None:
            answer = await client.post(
                f"/api/v1/jobs/{job_id}/answers", json={"blockId": block.id, "values": ["a"]}
            )
            assert answer.status_code == 204
            assert (await waiting).values == ["a"]
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


async def test_password_like_message_is_refused_while_a_secret_question_is_open(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    question = password_question(env)
    waiting = asyncio.create_task(ctx(env, user, job_id).ask_secret(question))
    try:
        await wait_pending(client, thread)
        for body in (
            {"text": "пароль123"},
            {"text": "пароль123", "clientMessageId": new_id()},
            {"text": "мой пароль: пароль123", "targetJobId": job_id},
        ):
            refused = await client.post(f"/api/v1/chat/threads/{thread}/messages", json=body)
            assert refused.status_code == 409
            assert refused.json()["detail"].startswith("Пароль и код вводятся только")
            assert "пароль123" not in refused.text
        async with env.sm() as session:
            messages = list(
                await session.scalars(select(ChatMessage).where(ChatMessage.thread_id == thread))
            )
            assert "пароль123" not in json.dumps([m.blocks for m in messages], ensure_ascii=False)
        assert await chat_jobs(env, thread) == []
        question_text = await post(client, thread, "что ты сейчас делаешь?")
        assert question_text.status_code == 202, question_text.text
        assert (await env.job(question_text.json()["jobId"])).kind == "chat"
        answer = await client.post(
            f"/api/v1/jobs/{job_id}/answers", json={"blockId": question.id, "values": "пароль123"}
        )
        assert answer.status_code == 204
        assert (await waiting).get_secret_value() == "пароль123"
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    history = await client.get(f"/api/v1/chat/threads/{thread}/messages")
    runtime = await client.get(f"/api/v1/chat/threads/{thread}/runtime")
    assert "пароль123" not in history.text + runtime.text
    assert "пароль123" not in json.dumps((await env.job(job_id)).payload, ensure_ascii=False)


async def test_second_message_waits_for_the_running_agent_turn_but_not_a_dialogue_answer(
    env: Env,
) -> None:
    client, user, thread, job_id = await start(env)
    first = await post(client, thread, "Привет")
    assert first.status_code == 202
    turn = first.json()["jobId"]
    busy = await post(client, thread, "Ещё вопрос")
    assert busy.status_code == 409
    assert busy.json()["detail"].startswith("Ассистент ещё отвечает")
    # A task's plain question is answered even while the agent turn runs.
    question = dialogue_question(env, "Какая неделя нужна?")
    waiting = asyncio.create_task(ctx(env, user, job_id).ask_dialogue(question))
    await wait_pending(client, thread)
    answer = await post(client, thread, "Текущая неделя")
    assert answer.status_code == 202
    assert answer.json()["jobId"] == job_id
    assert await waiting == "Текущая неделя"
    assert (await post(client, thread, "И ещё")).status_code == 409
    async with env.sm() as session:
        row = await session.get(Job, turn)
        assert row is not None
        row.state = "done"
        await session.commit()
    after = await post(client, thread, "Спасибо, а что дальше?")
    assert after.status_code == 202
    assert after.json()["jobId"] not in {turn, job_id}
    async with env.sm() as session:
        texts = [
            m.blocks[0]["markdown"]
            for m in await session.scalars(
                select(ChatMessage).where(
                    ChatMessage.thread_id == thread, ChatMessage.role == "user"
                )
            )
        ]
    assert sorted(texts) == sorted(["Привет", "Текущая неделя", "Спасибо, а что дальше?"])


async def test_target_job_must_be_a_live_task_of_this_thread(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    turn = (await post(client, thread, "Привет")).json()["jobId"]
    other_thread = (await client.post("/api/v1/chat/threads", json={})).json()["id"]
    _, foreign_task = await source_task(env, user, other_thread)
    for target in (turn, foreign_task, new_id()):
        response = await post(client, thread, "Уточнение", targetJobId=target)
        assert response.status_code == 404, target
    await finish(env, job_id)
    late = await post(client, thread, "Уточнение", targetJobId=job_id)
    assert late.status_code == 409
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.thread_id == thread, ChatMessage.role == "user")
            )
            == 1
        )


async def test_runtime_shows_tasks_pending_and_receipts_while_waiting_and_after_done(
    env: Env,
) -> None:
    client, user, thread, job_id = await start(env)
    empty = await runtime_of(client, thread)
    assert set(empty) == {"activeJob", "tasks", "pending", "receipts"}
    assert empty["activeJob"]["id"] == job_id
    assert empty["activeJob"]["state"] == "queued"
    worker = ctx(env, user, job_id)
    question = dialogue_question(env)
    waiting = asyncio.create_task(worker.ask_dialogue(question))
    state = await wait_pending(client, thread)
    assert state["activeJob"]["id"] == job_id
    assert state["activeJob"]["state"] == "waiting_user"
    assert [(t["id"], t["kind"], t["state"]) for t in state["tasks"]] == [
        (job_id, "connect", "waiting_user")
    ]
    assert state["pending"] == [
        {"jobId": job_id, "blockId": question.id, "channel": "dialogue", "secret": False}
    ]
    assert state["receipts"] == []
    sent = await post(client, thread, "Только расписание")
    message_id = sent.json()["message"]["id"]
    await waiting
    accepted = await runtime_of(client, thread)
    assert accepted["pending"] == []
    assert {(r["messageId"], r["blockId"], r["status"]) for r in accepted["receipts"]} == {
        (message_id, None, "accepted"),
        (None, question.id, "applied"),
    }
    await worker.drain_dialogue()
    await worker.checkpoint_learning(INTENT)
    await finish(env, job_id)
    done = await runtime_of(client, thread)
    assert done["activeJob"] is None  # a finished task is history, not the active job
    assert [(t["id"], t["state"]) for t in done["tasks"]] == [(job_id, "done")]
    assert done["pending"] == []
    assert {(r["messageId"], r["blockId"], r["status"]) for r in done["receipts"]} == {
        (message_id, None, "applied"),
        (None, question.id, "applied"),
    }
    history = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    assert history[-1]["blocks"][0]["markdown"] == "Источник подключён."
    assert history[-1]["jobId"] == job_id
    turn = await post(client, thread, "Что дальше?")
    assert turn.status_code == 202
    chatting = await runtime_of(client, thread)
    assert chatting["activeJob"]["id"] == turn.json()["jobId"]
    assert chatting["activeJob"]["kind"] == "chat"
    assert [t["id"] for t in chatting["tasks"]] == [job_id]


async def test_runtime_prefers_a_waiting_task_and_a_continued_child(env: Env) -> None:
    client, user, thread, _ = await start(env)
    now = env.clock.now()
    ids = {name: new_id() for name in ("waiting", "running", "parent", "child")}
    async with env.sm() as session:
        session.add_all(
            [
                Job(
                    id=ids["waiting"],
                    user_id=user,
                    thread_id=thread,
                    kind="sync",
                    state="waiting_user",
                    payload={},
                    run_after=now,
                ),
                Job(
                    id=ids["running"],
                    user_id=user,
                    thread_id=thread,
                    kind="repair",
                    state="running",
                    payload={},
                    run_after=now,
                ),
            ]
        )
        await session.commit()
    state = await runtime_of(client, thread)
    assert state["activeJob"]["id"] == ids["waiting"]
    async with env.sm() as session:
        for name in ("waiting", "running"):
            row = await session.get(Job, ids[name])
            assert row is not None
            row.state = "done"
        await session.flush()
        session.add(
            Job(
                id=ids["child"],
                user_id=user,
                thread_id=thread,
                kind="connect",
                state="queued",
                payload={},
                run_after=now,
            )
        )
        await session.flush()
        session.add(
            Job(
                id=ids["parent"],
                user_id=user,
                thread_id=thread,
                kind="connect",
                state="waiting_user",
                payload={"continuation_job_id": ids["child"]},
                run_after=now,
            )
        )
        await session.commit()
    state = await runtime_of(client, thread)
    assert state["activeJob"]["id"] == ids["child"]
    assert len(state["tasks"]) == 5


async def test_tasks_of_different_threads_do_not_mix(env: Env) -> None:
    client, user, first, first_job = await start(env)
    second = (await client.post("/api/v1/chat/threads", json={})).json()["id"]
    second_job = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=user, thread_id=second, payload={}, dedupe=False
    )
    question = dialogue_question(env)
    waiting = asyncio.create_task(ctx(env, user, first_job).ask_dialogue(question))
    try:
        await wait_pending(client, first)
        listed = {t["id"]: t for t in (await client.get("/api/v1/chat/threads")).json()}
        assert listed[first]["attention"] == "waiting_user"
        assert listed[first]["activeTask"]["jobId"] == first_job
        assert listed[second]["attention"] == "running"
        assert listed[second]["activeTask"]["jobId"] == second_job
        other = await runtime_of(client, second)
        assert [t["id"] for t in other["tasks"]] == [second_job]
        assert other["pending"] == []
        elsewhere = await post(client, second, "Что у меня завтра?")
        assert elsewhere.status_code == 202
        assert (await env.job(elsewhere.json()["jobId"])).thread_id == second
        assert (await env.job(elsewhere.json()["jobId"])).kind == "chat"
        foreign = await post(client, second, "Ответ", targetJobId=first_job)
        assert foreign.status_code == 404
        assert dialogue.state_of(await env.job(first_job)).get("inbox") in (None, [])
        answered = await post(client, first, "Только расписание")
        assert answered.json()["jobId"] == first_job
        assert await waiting == "Только расписание"
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    second_history = (await client.get(f"/api/v1/chat/threads/{second}/messages")).json()
    assert question.id not in json.dumps(second_history)
    assert [m["blocks"][0]["markdown"] for m in second_history] == ["Что у меня завтра?"]
    assert await chat_jobs(env, first) == []


# --- dialogue and history -----------------------------------------------------------------------


async def test_dialogue_answer_and_checkpoint_are_replay_safe(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    worker = ctx(env, user, job_id)
    question = dialogue_question(env)
    waiting = asyncio.create_task(worker.ask_dialogue(question))
    await wait_pending(client, thread)
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
    runtime = await runtime_of(client, thread)
    assert runtime["pending"] == []
    assert any(
        r["messageId"] == one.json()["message"]["id"] and r["status"] == "applied"
        for r in runtime["receipts"]
    )
    job = await env.job(job_id)
    assert job.payload["learning_intent"]["absent_kinds"] == ["deadlines"]
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Job).where(Job.thread_id == thread)
            )
            == 1
        )
    retry = await client.post(f"/api/v1/chat/threads/{thread}/messages", json=body)
    assert retry.status_code == 202
    assert retry.json() == one.json()
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


async def test_explicit_clarification_clears_automatic_discovery_of_the_task(env: Env) -> None:
    _, user, _, job_id = await start(
        env,
        payload={"learning_intent": intent_of(normalize_context({"entry_urls": [URL]}))},
    )
    assert (await env.job(job_id)).payload["learning_intent"]["discovery_mode"] == "auto"
    await ctx(env, user, job_id).checkpoint_learning(
        {
            "entry_urls": [URL],
            "requested_kinds": ["courses"],
            "absent_kinds": [],
            "user_note": "Только курсы",
        }
    )
    intent = (await env.job(job_id)).payload["learning_intent"]
    assert intent.get("discovery_mode", "requested") == "requested"
    assert intent["requested_kinds"] == ["courses"]
    assert LearningIntent.restore(intent).discovery_mode == "requested"


async def test_native_secret_never_enters_composer_history_or_receipts(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    worker = ctx(env, user, job_id)
    question = password_question(env)
    waiting = asyncio.create_task(worker.ask_secret(question))
    await wait_pending(client, thread)
    response = await post(client, thread, SECRET, targetJobId=job_id)
    assert response.status_code == 409
    implicit = await post(client, thread, SECRET)
    assert implicit.status_code == 409
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
    assert await chat_jobs(env, thread) == []
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
    await post(client, thread, "Дедлайнов нет", targetJobId=job_id)
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
    response = await post(client, thread, "a" * 4500 + " " + bad, targetJobId=job_id)
    assert response.status_code == 422
    assert SECRET not in response.text
    # A message routed to the task's question without an explicit target is checked the same way.
    async with env.sm() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        dialogue.set_pending(job, dialogue_question(env), channel="dialogue", now=env.clock.now())
        job.state = "waiting_user"
        await session.commit()
    response = await post(client, thread, "Вот ссылка " + bad)
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


async def test_terminal_history_survives_event_purge_and_a_message_starts_the_agent(
    env: Env,
) -> None:
    client, _user, thread, job_id = await start(env)
    worker = env.worker({})
    claimed = await worker.claim()
    assert claimed
    assert claimed.id == job_id
    await worker._finish(claimed, "done", result={"status": "error", "detail": "not_found"})  # pyright: ignore[reportPrivateUsage]
    env.advance(minutes=11)
    async with env.sm() as session:
        await purge_finished(session, env.clock.now())
        await session.commit()
    state = await runtime_of(client, thread)
    assert state["activeJob"] is None
    assert [(t["id"], t["state"]) for t in state["tasks"]] == [(job_id, "done")]
    assert state["pending"] == []
    history = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    assert any("Подключение не завершено" in m["blocks"][0].get("markdown", "") for m in history)
    assert (await post(client, thread, "late", targetJobId=job_id)).status_code == 409
    normal = await post(client, thread, "Что делать дальше?")
    assert normal.status_code == 202
    state = await runtime_of(client, thread)
    assert state["activeJob"]["kind"] == "chat"
    assert state["activeJob"]["id"] == normal.json()["jobId"]
    other = env.new_client()
    await env.account(other)
    assert (await other.get(f"/api/v1/jobs/{normal.json()['jobId']}")).status_code == 404


async def test_cancel_rejects_late_dialogue_and_closes_pending(env: Env) -> None:
    client, user, thread, job_id = await start(env)
    waiting = asyncio.create_task(
        ctx(env, user, job_id).ask_dialogue(dialogue_question(env, "Ссылка?"))
    )
    await wait_pending(client, thread)
    assert (await client.post(f"/api/v1/jobs/{job_id}/cancel")).status_code == 204
    with pytest.raises(JobCancelled):
        await waiting
    state = await runtime_of(client, thread)
    assert state["pending"] == []
    assert state["activeJob"] is None
    assert [(t["id"], t["state"]) for t in state["tasks"]] == [(job_id, "cancelled")]
    history = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    assert history[-1]["blocks"][0]["markdown"] == "Подключение отменено."
    assert (await post(client, thread, "Вот ссылка", targetJobId=job_id)).status_code == 409
    after = await post(client, thread, "Вот ссылка")
    assert after.status_code == 202
    assert (await env.job(after.json()["jobId"])).kind == "chat"


# --- a connection's thread ----------------------------------------------------------------------


async def test_attach_reuses_the_task_thread_and_sync_follows_it(env: Env) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    space, connection = await env.connection(user, status="error")
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=user, connection_id=connection
    )
    body = {"jobId": job_id}
    response = await client.post(f"/api/v1/connections/{connection}/conversation", json=body)
    assert response.status_code == 200
    attached = response.json()
    assert set(attached) == THREAD_OUT_KEYS
    assert attached["title"] == "Источник"
    assert attached["spaceId"] == space
    assert attached["activeTask"]["jobId"] == job_id
    assert attached["activeTask"]["kind"] == "connect"
    thread = attached["id"]
    again = await client.post(f"/api/v1/connections/{connection}/conversation", json=body)
    assert again.json()["id"] == thread
    without_job = await client.post(f"/api/v1/connections/{connection}/conversation", json={})
    assert without_job.json()["id"] == thread
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
    sync_id = sync.json()["jobId"]
    assert (await env.job(sync_id)).thread_id == thread  # the thread of the newest task
    linked = await client.post(
        f"/api/v1/connections/{connection}/conversation", json={"jobId": sync_id}
    )
    assert linked.json()["id"] == thread
    state = await runtime_of(client, thread)
    assert state["activeJob"]["kind"] == "sync"
    assert [t["id"] for t in state["tasks"]] == [sync_id, job_id]
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ChatThread).where(ChatThread.user_id == user)
            )
            == 1
        )
    other = env.new_client()
    await env.account(other)
    assert (
        await other.post(f"/api/v1/connections/{connection}/conversation", json=body)
    ).status_code == 404


async def test_attach_replays_safe_legacy_updates_and_rejects_mismatch_disabled(env: Env) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    _, connection = await env.connection(user)
    _, foreign_connection = await env.connection(user)
    job_id = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=user, connection_id=connection
    )
    block = ThinkingBlock(id=new_id(), at=env.clock.now(), stage="Проверяю сайт")
    login = LoginBlock(
        id=new_id(),
        at=env.clock.now(),
        connection_id=connection,
        ws_url="wss://screen.invalid/secret-ticket",
        ticket="fixture-ticket-do-not-store",
        expires_at=env.clock.now() + timedelta(minutes=5),
    )
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
        await append_event(
            session,
            job_id,
            "block",
            login.model_dump(by_alias=True, mode="json", exclude_none=True),
        )
        await session.commit()
    response = await client.post(
        f"/api/v1/connections/{connection}/conversation", json={"jobId": job_id}
    )
    assert response.status_code == 200
    thread = response.json()["id"]
    history = await client.get(f"/api/v1/chat/threads/{thread}/messages")
    rows = history.json()
    assert [m["blocks"][0]["id"] for m in rows] == [block.id, login.id]
    assert rows[0]["blocks"][0]["state"] == "ok"
    assert rows[1]["blocks"][0]["type"] == "text"
    assert "fixture-ticket-do-not-store" not in history.text
    assert "secret-ticket" not in history.text
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
    assert (await post(client, thread, "hi", targetJobId=job_id)).status_code == 403


# --- continuation on another site ---------------------------------------------------------------


async def test_cross_host_continuation_is_same_chat_but_separate_connection_and_secrets(
    env: Env,
) -> None:
    client, user, thread = await student(env)
    parent, parent_job = await source_task(env, user, thread)
    await env.services.vault.put(user, parent, SecretKind.PASSWORD, SECRET)
    before_handoff = {
        "text": "Расписание нужно за эту неделю",
        "targetJobId": parent_job,
        "clientMessageId": new_id(),
    }
    accepted = await client.post(f"/api/v1/chat/threads/{thread}/messages", json=before_handoff)
    assert accepted.status_code == 202
    handoff = ctx(env, user, parent_job, parent)
    turns = await handoff.drain_dialogue()
    assert [turn.text for turn in turns] == [before_handoff["text"]]
    await handoff.checkpoint_learning(INTENT)
    result = await continue_source(handoff, env.services, parent, CHILD_URL, INTENT)
    child = await env.job(result["jobId"])
    assert child.thread_id == thread
    assert child.connection_id == result["connectionId"]
    assert child.payload["url"] == CHILD_URL
    assert child.payload["parent_job_id"] == parent_job
    assert not await env.services.vault.has(user, result["connectionId"], SecretKind.PASSWORD)
    async with env.sm() as session:
        origin = await session.get(Connection, parent)
        target = await session.get(Connection, result["connectionId"])
        assert origin
        assert target
        assert origin.space_id == target.space_id
        assert origin.origin == ORIGIN
        assert origin.allow_hosts == ["schedule.fakeuni.test"]
        assert target.origin == "https://study.fakeuni.test"
        assert target.allow_hosts == ["study.fakeuni.test"]
    state = await runtime_of(client, thread)
    assert state["activeJob"]["id"] == child.id
    assert state["activeJob"]["connectionId"] == result["connectionId"]
    assert {t["id"] for t in state["tasks"]} >= {child.id, parent_job}
    current = await client.post(
        f"/api/v1/chat/threads/{thread}/messages",
        json={
            "text": "Продолжаю по новой ссылке",
            "targetJobId": child.id,
            "clientMessageId": new_id(),
        },
    )
    assert current.status_code == 202
    assert current.json()["jobId"] == child.id
    replay = await client.post(f"/api/v1/chat/threads/{thread}/messages", json=before_handoff)
    assert replay.status_code == 202
    assert replay.json() == accepted.json()


@pytest.mark.parametrize("competing_kind", [JobKind.CONNECT, JobKind.SYNC])
async def test_continuation_waits_for_source_group_before_locking_existing_child(
    env: Env, monkeypatch: pytest.MonkeyPatch, competing_kind: JobKind
) -> None:
    _client, user, thread = await student(env)
    parent, parent_job = await source_task(env, user, thread)
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


async def test_unthreaded_continuation_creates_one_thread_before_ui_attach(env: Env) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    space, parent = await env.connection(user, status="draft", origin=ORIGIN)
    parent_job = await env.services.queue.enqueue(
        JobKind.CONNECT, user_id=user, connection_id=parent
    )
    result = await continue_source(
        ctx(env, user, parent_job, parent), env.services, parent, CHILD_URL, INTENT
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
    assert attached.json()["title"] == "Источник"
    assert attached.json()["spaceId"] == space
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
    _, user, thread = await student(env)
    first, first_job = await source_task(env, user, thread)
    async with env.sm() as session:
        parent = await session.get(Connection, first)
        assert parent is not None
        space = parent.space_id
    second, second_job = await source_task(
        env, user, thread, origin="https://other.fakeuni.test", space=space
    )
    async with asyncio.timeout(5):
        one, two = await asyncio.gather(
            continue_source(
                ctx(env, user, first_job, first), env.services, first, CHILD_URL, INTENT
            ),
            continue_source(
                ctx(env, user, second_job, second), env.services, second, CHILD_URL, INTENT
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


# --- repair and login from the agent ------------------------------------------------------------


async def test_chat_repair_resumes_unconfigured_connect_in_the_current_thread(env: Env) -> None:
    client = env.new_client()
    user = (await env.account(client)).id
    _, connection = await env.connection(user, status="error")
    thread = (await client.post("/api/v1/chat/threads", json={})).json()["id"]
    tools = ChatEnv(
        services=env.services, user_id=user, thread_id=thread, job_id=None, space_id=None
    )
    result = await request_repair(
        tools, {"connection_id": connection, "reason": "Данные ещё не появились"}
    )
    assert "Подключение" in result
    assert "в этом чате" in result
    state = await runtime_of(client, thread)
    assert state["activeJob"]["kind"] == "connect"
    job_id = state["activeJob"]["id"]
    assert job_id not in result
    assert (await env.job(job_id)).thread_id == thread
    assert [block.type for block in tools.blocks] == ["task"]
    again = await request_repair(tools, {"connection_id": connection})
    assert "уже идёт в этом чате" in again
    other_thread = (await client.post("/api/v1/chat/threads", json={})).json()["id"]
    elsewhere = ChatEnv(
        services=env.services, user_id=user, thread_id=other_thread, job_id=None, space_id=None
    )
    redirected = await request_repair(elsewhere, {"connection_id": connection})
    assert "уже идёт" in redirected
    assert thread_link(thread) in redirected
    assert job_id not in redirected
    assert (await env.job(job_id)).thread_id == thread  # a running task is never moved
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
    assert claimed.id == job_id
    await worker._fail(claimed, "login_failed", "Нужно продолжить вход", 400)  # pyright: ignore[reportPrivateUsage]
    history = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    assert any("Нужно продолжить вход" in m["blocks"][0].get("markdown", "") for m in history)
    other_history = (await client.get(f"/api/v1/chat/threads/{other_thread}/messages")).json()
    assert other_history == []


@pytest.mark.parametrize("status", ["error", "needs_reauth"])
@pytest.mark.parametrize("requested", [None, ["scheduleEvents"]])
async def test_manual_login_resumes_connect_in_the_current_thread_and_preserves_intent(
    env: Env, status: str, requested: list[str] | None
) -> None:
    context: dict[str, Any] = {"entry_urls": [URL], "absent_kinds": ["deadlines"]}
    if requested is not None:
        context["requested_kinds"] = requested
    client, user, thread = await student(env)
    connection, old_id = await source_task(env, user, thread, context=context)
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
    runtime = await runtime_of(client, other_thread)
    resumed_id = runtime["activeJob"]["id"]
    assert resumed_id != old_id
    resumed = await env.job(resumed_id)
    assert resumed.kind == "connect"
    assert resumed.thread_id == other_thread
    assert resumed.payload["url"] == URL
    assert resumed.payload["manual_login"] is True
    assert resumed.payload["source"] == "chat"
    intent = LearningIntent.restore(resumed.payload["learning_intent"])
    assert intent.discovery_mode == ("auto" if requested is None else "requested")
    assert intent.entry_urls == (URL,)
    assert intent.absent_kinds == ("deadlines",)
    for result in results:
        assert "Запрос защищённого входа" in result
        assert "в этом чате" in result
        assert resumed_id not in result
    assert (await runtime_of(client, thread))["activeJob"] is None
    async with env.sm() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Job).where(Job.connection_id == connection)
            )
            == 2
        )


@pytest.mark.parametrize(
    ("kind", "state"),
    [("connect", "waiting_user"), ("repair", "queued"), ("sync", "running")],
)
@pytest.mark.parametrize("asked_in", ["same", "other"])
async def test_manual_login_returns_active_source_without_replacing_it(
    env: Env, kind: str, state: str, asked_in: str
) -> None:
    client, user, thread = await student(env)
    connection, old_id = await source_task(env, user, thread)
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
                active_job, password_question(env), channel="native", now=env.clock.now()
            )
            active_payload = dict(active_job.payload)
        session.add(active_job)
        await session.commit()
    current = (
        thread
        if asked_in == "same"
        else (await client.post("/api/v1/chat/threads", json={})).json()["id"]
    )
    tools = ChatEnv(
        services=env.services, user_id=user, thread_id=current, job_id=None, space_id=None
    )
    result = await request_login(tools, {"connection_id": connection})
    assert "уже идёт" in result
    assert "Новый вход не запущен" in result
    assert active_id not in result
    if asked_in == "same":
        assert "в этом чате" in result
    else:
        assert thread_link(thread) in result
    active = await env.job(active_id)
    assert active.kind == kind
    assert active.state == state
    assert active.thread_id == thread
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
    assert (await runtime_of(client, thread))["activeJob"]["id"] == active_id
    if asked_in == "other":
        assert (await runtime_of(client, current))["tasks"] == []


async def test_manual_login_rejects_foreign_missing_and_disabled_connections(env: Env) -> None:
    client, user, thread = await student(env)
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


async def test_manual_login_waits_for_the_running_source_without_locking_the_thread(
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


# --- a retried CONNECT keeps its own intent -----------------------------------------------------


@pytest.mark.parametrize("continued", [False, True])
async def test_unconfigured_repair_restores_own_intent_url_query_and_current_job(
    env: Env, continued: bool
) -> None:
    client, user, thread = await student(env)
    parent, first_job = await source_task(
        env,
        user,
        thread,
        context={
            "entry_urls": [URL],
            "absent_kinds": ["deadlines"],
            "user_note": INTENT["user_note"],
        },
    )
    await env.services.vault.put(user, parent, SecretKind.PASSWORD, SECRET)
    if continued:
        # The child's different intent lives on its own job and never leaks into the parent.
        await continue_source(
            ctx(env, user, first_job, parent),
            env.services,
            parent,
            "https://child.fakeuni.test/courses",
            {
                "entry_urls": ["https://child.fakeuni.test/courses"],
                "requested_kinds": ["grades"],
                "absent_kinds": ["scheduleEvents"],
                "user_note": "Оценки на другом сайте",
            },
        )
    async with env.sm() as session:
        old = await session.get(Job, first_job)
        conn = await session.get(Connection, parent)
        assert old is not None
        assert conn is not None
        old.state = "done"
        old.result = {"status": "error", "detail": "not_found"}
        old.finished_at = env.clock.now()
        conn.status = "error"
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
    runtime = await runtime_of(client, thread)
    assert runtime["activeJob"]["id"] == job_id
    stale = await post(client, thread, "старый ответ", targetJobId=first_job)
    assert stale.status_code == 409
    current = await post(client, thread, "Продолжаю с этой ссылки", targetJobId=job_id)
    assert current.status_code == 202
    assert current.json()["jobId"] == job_id


async def test_unthreaded_connect_retry_retains_both_kinds_and_own_url(env: Env) -> None:
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
