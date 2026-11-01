"""v4 CHAT through the real Worker/Postgres: answer, tool call, ask round-trip, parity guards.

The flag makes the CHAT job run on the v4 free harness (`agent.v4.chat`). These tests drive the same
product path a phone client does: post a message through the API, run the real worker, read the
thread and the job events. The model is a scripted provider (no keys, no network except fakeuni),
the tools are the real ones (real DB, real LocalExecutor, fakeuni over an ASGI transport).
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from botai.agent.v4.chat import make_chat_handlers
from botai.core.errors import ErrorCode
from botai.core.ids import new_id
from botai.core.llm import LlmRequest, LlmResponse
from botai.db.models import AgentRun, AgentRunEvent, ChatMessage, ChatThread, Space
from botai.sources.v4 import service as sources_service
from tests.agent.v4.runtime_support import answer, call, tool_response
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env

pytestmark = pytest.mark.db

BASE = "https://fakeuni.test"
SECRET = "S3cr3t-FIXTURE-Value-9137"


class FlowProvider:
    """A scripted provider that answers from what the run already did (steps, not fingerprints)."""

    name = "flow"

    def __init__(self, respond: Callable[[LlmRequest], Awaitable[LlmResponse]]) -> None:
        self._respond = respond
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return await self._respond(request)

    async def aclose(self) -> None:
        return None


def _steps(request: LlmRequest) -> int:
    return sum(1 for m in request.messages if m.role == "assistant" and m.tool_calls)


def _dumped(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


@pytest.fixture
def fakeuni_cookies() -> dict[str, str]:
    token = secrets.token_hex(16)
    fakeuni_login.SESSIONS.add(token)
    return {fakeuni_login.COOKIE: token}


@pytest.fixture
def fakeuni_transport(fakeuni_app: Any) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=fakeuni_app)


async def _student(
    env: Env, *, with_space: bool = False
) -> tuple[httpx.AsyncClient, str, str, str | None]:
    """(client, user_id, thread_id, space_id) — a logged-in student with an empty chat."""
    client = env.new_client()
    account = await env.account(client)
    body: dict[str, str] = {}
    space_id: str | None = None
    if with_space:
        space = await client.post("/api/v1/spaces", json={"title": "Тестовый вуз"})
        assert space.status_code in (200, 201), space.text
        space_id = space.json()["id"]
        body["spaceId"] = space_id
    thread = await client.post("/api/v1/chat/threads", json=body)
    assert thread.status_code == 201, thread.text
    return client, account.id, thread.json()["id"], space_id


async def _post(client: httpx.AsyncClient, thread: str, text: str, **extra: Any) -> httpx.Response:
    return await client.post(
        f"/api/v1/chat/threads/{thread}/messages", json={"text": text, **extra}
    )


async def _source(env: Env, user_id: str, space_id: str) -> str:
    """A v4 source in the thread's space: what the agent may read in the chat."""
    async with env.sm() as session:
        source = await sources_service.create_source(
            session,
            user_id=user_id,
            space_id=space_id,
            title="Личный кабинет",
            entry_url=f"{BASE}/app",
            kinds=["lms", "schedule"],
        )
        await session.commit()
        return source.id


async def _chat_worker(
    env: Env,
    provider: Any,
    *,
    transport: httpx.ASGITransport | None = None,
    cookies: dict[str, str] | None = None,
    tmp_path: Path | None = None,
) -> Any:
    env.settings.agent_engine = "v4"
    return env.worker(
        make_chat_handlers(
            provider=provider,
            http_transport=transport,
            source_cookies=cookies,
            artifact_root=str(tmp_path / "artifacts") if tmp_path is not None else None,
            with_browser=False,
        )
    )


async def _last_message(env: Env, thread: str, *, role: str = "agent") -> ChatMessage | None:
    async with env.sm() as session:
        return await session.scalar(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread, ChatMessage.role == role)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )


async def _runtime(client: httpx.AsyncClient, thread: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/chat/threads/{thread}/runtime")
    assert response.status_code == 200, response.text
    return response.json()


async def _thread_messages(client: httpx.AsyncClient, thread: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/chat/threads/{thread}/messages")
    assert response.status_code == 200, response.text
    return response.json()


# --- a tool call, the answer and the thread -----------------------------------------------------


async def test_chat_v4_answers_with_a_tool_call_and_stores_the_reply(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
) -> None:
    env.settings.agent_engine = "v4"
    client, user, thread, space = await _student(env, with_space=True)
    assert space is not None
    source_id = await _source(env, user, space)

    async def respond(request: LlmRequest) -> LlmResponse:
        if _steps(request) == 0:
            return tool_response(
                call(
                    "source_read_live",
                    {"source_id": source_id, "url": f"{BASE}/api/v2/me"},
                    "c1",
                )
            )
        return answer("В вашем профиле: Иванова Анна, 3 курс, прикладная математика.")

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "Кто я в личном кабинете?")
    assert sent.status_code == 202, sent.text
    job_id = sent.json()["jobId"]
    env.start(
        await _chat_worker(
            env,
            provider,
            transport=fakeuni_transport,
            cookies=fakeuni_cookies,
            tmp_path=tmp_path,
        )
    )
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert job.result is not None
    result = job.result
    assert result["stopped"] == "answer"
    assert result["messageId"]
    assert "source_read_live" in result["tools"]
    assert result["sources"]
    assert result["sources"][0]["title"] == "Личный кабинет"
    assert set(result["context"]) == {"system", "profile", "data", "documents", "history"}
    assert result["context"]["data"] > 0, "a tool result must be visible in the context meter"
    assert result["steps"] == 2

    # The fakeuni profile really reached the model as tool data (not invented by the answer).
    second = provider.requests[1]
    tool_messages = [m.content for m in second.messages if m.role == "tool"]
    assert any("Иванова Анна" in text for text in tool_messages)
    assert any('<source_data tool="source_read_live">' in text for text in tool_messages)

    # The thread got the answer with its source card, after a thinking line (v3 chat shape).
    stored = await _last_message(env, thread)
    assert stored is not None
    assert stored.job_id == job_id
    (block,) = stored.blocks
    assert block["type"] == "text"
    assert "Иванова Анна" in block["markdown"]
    assert block["sources"]
    assert block["sources"][0]["title"] == "Личный кабинет"

    history = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    assert [message["role"] for message in history] == ["user", "agent", "agent"]
    assert history[-2]["blocks"][0]["type"] == "thinking"
    assert history[-1]["blocks"][0]["type"] == "text"

    # Job SSE events: the thinking block, the text block and the terminal done event.
    events = await env.events(job_id)
    kinds = [(event.type, event.data.get("type")) for event in events]
    assert ("block", "thinking") in kinds
    assert ("block", "text") in kinds
    assert ("done", None) in kinds

    # The run is a durable trace of the v4 runtime (D-014), not only a chat message.
    async with env.sm() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.user_id == user))
    assert run is not None
    assert run.status == "answer"

    async with env.sm() as session:
        thread_row = await session.get(ChatThread, thread)
    assert thread_row is not None
    assert thread_row.title == "Кто я в личном кабинете?"


# --- an account without universities creates its first one in the chat --------------------------


async def test_chat_v4_creates_the_university_the_student_names(env: Env, tmp_path: Path) -> None:
    """The production edge case: no space, no form in the UI, so the chat agent makes the space."""
    env.settings.agent_engine = "v4"
    client, user, thread, space = await _student(env)
    assert space is None

    async def respond(request: LlmRequest) -> LlmResponse:
        if _steps(request) == 0:
            return tool_response(call("space_create", {"title": "Тестовый университет"}, "c1"))
        return answer(
            "Создал вуз «Тестовый университет». Скажите ссылку на личный кабинет — подключу."
        )

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "У меня нет вуза. Подключи Тестовый университет")
    assert sent.status_code == 202, sent.text
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert job.result is not None
    assert job.result["stopped"] == "answer"
    assert "space_create" in job.result["tools"]

    # The chat turn really made the university, and it is the student's own space.
    async with env.sm() as session:
        spaces = list(await session.scalars(select(Space).where(Space.user_id == user)))
    assert len(spaces) == 1
    assert spaces[0].university_id is not None
    assert spaces[0].title

    # The tool result reached the model as data: the next step sees the new space_id.
    second = provider.requests[1]
    tool_text = "\n".join(m.content for m in second.messages if m.role == "tool")
    assert spaces[0].id in tool_text
    assert "space_create" in tool_text

    # The user-facing prompt knows the real UI and that it creates universities itself.
    system = provider.requests[0].messages[0]
    assert system.role == "system"
    assert "«Учёба»" in system.content
    assert "space_create" in system.content
    assert "Отдельной формы или кнопки «создать вуз» в приложении нет" in system.content

    stored = await _last_message(env, thread)
    assert stored is not None
    block = stored.blocks[0]
    assert block["type"] == "text"
    assert "Тестовый университет" in block["markdown"]


# --- ask round-trip -----------------------------------------------------------------------------


async def test_chat_v4_asks_the_student_in_the_dialogue_and_continues_after_the_answer(
    env: Env, tmp_path: Path
) -> None:
    env.settings.agent_engine = "v4"
    client, _user, thread, _space = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:
        if _steps(request) == 0:
            return tool_response(
                call("ask_user", {"question": "Какой вуз открыть?", "input_kind": "text"}, "c1")
            )
        return answer("Открываю ВШЭ: скажите, что именно показать — расписание или задания.")

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "Помоги с вузом")
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))

    waiting = await env.wait_for(job_id, {"waiting_user"}, within=30)
    assert waiting.state == "waiting_user"
    blocks = [
        event.data
        for event in await env.events(job_id)
        if event.type == "block" and event.data.get("type") == "input"
    ]
    assert blocks
    assert blocks[-1]["question"] == "Какой вуз открыть?"

    # The parked turn is visible in the thread runtime exactly like a task's question, and a plain
    # text question of a chat turn is a dialogue question: the composer answers it, the run
    # continues. (A question with no answer path was the bug.)
    runtime = await _runtime(client, thread)
    assert runtime["activeJob"]["id"] == job_id
    assert runtime["activeJob"]["state"] == "waiting_user"
    assert runtime["pending"] == [
        {"jobId": job_id, "blockId": blocks[-1]["id"], "channel": "dialogue", "secret": False}
    ]
    # The question is a turn of the conversation, not a transient job event: it is stored in the
    # thread, and the answer quotes it.
    history = await _thread_messages(client, thread)
    assert history[-1]["role"] == "agent"
    assert history[-1]["blocks"][0]["markdown"] == "Какой вуз открыть?"

    answered = await _post(client, thread, "ВШЭ", targetJobId=job_id)
    assert answered.status_code == 202, answered.text
    assert answered.json()["jobId"] == job_id
    history = await _thread_messages(client, thread)
    reply = [message for message in history if message["role"] == "user"][-1]
    assert reply["blocks"][0]["markdown"] == "ВШЭ"
    assert reply["blocks"][0]["quote"]["text"] == "Какой вуз открыть?"

    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    assert job.result["stopped"] == "answer"
    stored = await _last_message(env, thread)
    assert stored is not None
    assert "ВШЭ" in stored.blocks[0]["markdown"]
    # The student's answer went into the model context as tool data of ask_user.
    second = provider.requests[1]
    tool_text = "\n".join(m.content for m in second.messages if m.role == "tool")
    assert "ВШЭ" in tool_text


@pytest.mark.parametrize("answer_channel", ["composer", "block"])
@pytest.mark.parametrize("pasted_password", [False, True])
async def test_chat_v4_keeps_login_email_in_the_answer_and_next_model_prompt(
    env: Env, tmp_path: Path, answer_channel: str, pasted_password: bool
) -> None:
    env.settings.agent_engine = "v4"
    client, _user, thread, _space = await _student(env)
    email = "student.42@example.test"
    reply = f"{email}; пароль: {SECRET}" if pasted_password else email
    expected = f"{email}; пароль: <secret>" if pasted_password else email

    async def respond(request: LlmRequest) -> LlmResponse:
        if _steps(request) == 0:
            return tool_response(
                call("ask_user", {"question": "Какой email для входа?", "input_kind": "text"}, "c1")
            )
        return answer("Логин получен, продолжаю вход.")

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "Подключи кабинет")
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))
    await env.wait_for(job_id, {"waiting_user"}, within=30)
    pending = (await _runtime(client, thread))["pending"][0]
    assert pending["channel"] == "dialogue"
    client_message_id = new_id()
    if answer_channel == "composer":
        answered = await _post(
            client, thread, reply, targetJobId=job_id, clientMessageId=client_message_id
        )
        assert answered.status_code == 202, answered.text
    else:
        answered = await client.post(
            f"/api/v1/jobs/{job_id}/answers",
            json={"blockId": pending["blockId"], "values": reply},
        )
        assert answered.status_code == 204, answered.text
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    assert job.result["stopped"] == "answer"
    history = await _thread_messages(client, thread)
    replies = [message for message in history if message["role"] == "user"]
    assert replies[-1]["blocks"][0]["markdown"] == expected
    assert SECRET not in _dumped(history)
    tool_text = "\n".join(m.content for m in provider.requests[1].messages if m.role == "tool")
    assert email in tool_text
    assert "<email>" not in tool_text
    assert SECRET not in _dumped([request.model_dump() for request in provider.requests])

    if answer_channel == "composer":
        replay = await _post(
            client, thread, reply, targetJobId=job_id, clientMessageId=client_message_id
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["message"]["id"] == answered.json()["message"]["id"]

    # A later turn must read the usable login from durable history as well.
    sent_again = await _post(client, thread, "Продолжи с тем же логином")
    await env.wait_for(sent_again.json()["jobId"], {"waiting_user"}, within=30)
    assert email in "\n".join(m.content for m in provider.requests[2].messages)
    stopped = await client.post(f"/api/v1/jobs/{sent_again.json()['jobId']}/cancel")
    assert stopped.status_code == 204


async def test_chat_v4_plain_reply_answers_the_open_question_without_a_target(
    env: Env, tmp_path: Path
) -> None:
    """A message typed into the field needs no `targetJobId` to reach the waiting turn."""
    env.settings.agent_engine = "v4"
    client, _user, thread, _space = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:
        if _steps(request) == 0:
            return tool_response(
                call("ask_user", {"question": "Какой вуз открыть?", "input_kind": "text"}, "c1")
            )
        return answer("Открываю ВШЭ.")

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "Помоги с вузом")
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))
    await env.wait_for(job_id, {"waiting_user"}, within=30)

    answered = await _post(client, thread, "ВШЭ")
    assert answered.status_code == 202, answered.text
    assert answered.json()["jobId"] == job_id
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None


async def test_chat_v4_choice_question_stays_a_card_and_keeps_the_question(
    env: Env, tmp_path: Path
) -> None:
    env.settings.agent_engine = "v4"
    client, _user, thread, _space = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:
        if _steps(request) == 0:
            return tool_response(
                call(
                    "ask_user",
                    {
                        "question": "Что показать?",
                        "input_kind": "choice",
                        "options": ["Расписание", "Задания"],
                    },
                    "c1",
                )
            )
        return answer("Показываю задания.")

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "Покажи данные")
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))
    await env.wait_for(job_id, {"waiting_user"}, within=30)

    blocks = [
        event.data
        for event in await env.events(job_id)
        if event.type == "block" and event.data.get("type") == "choice"
    ]
    assert blocks
    # A choice is not a conversation turn: it is a card above the composer, answered by a block.
    runtime = await _runtime(client, thread)
    assert runtime["pending"] == [
        {"jobId": job_id, "blockId": blocks[-1]["id"], "channel": "native", "secret": False}
    ]
    history = await _thread_messages(client, thread)
    assert history[-1]["blocks"][0]["markdown"] == "Что показать?"

    answered = await client.post(
        f"/api/v1/jobs/{job_id}/answers",
        json={"blockId": blocks[-1]["id"], "values": "1"},
    )
    assert answered.status_code == 204, answered.text
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    tool_text = "\n".join(m.content for m in provider.requests[1].messages if m.role == "tool")
    assert "Задания" in tool_text


async def test_chat_v4_waiting_turn_stops_and_frees_the_composer(env: Env, tmp_path: Path) -> None:
    env.settings.agent_engine = "v4"
    client, _user, thread, _space = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:
        if _steps(request) == 0:
            return tool_response(
                call("ask_user", {"question": "Какой вуз открыть?", "input_kind": "text"}, "c1")
            )
        return answer("Открываю ВШЭ.")

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "Помоги с вузом")
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))
    await env.wait_for(job_id, {"waiting_user"}, within=30)
    assert (await _runtime(client, thread))["pending"]

    # «Остановить» is a normal outcome, not an error: the question stops being asked and the
    # thread has nothing pending any more.
    stopped = await client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert stopped.status_code == 204, stopped.text
    job = await env.wait_for(job_id, {"cancelled"}, within=30)
    assert job.state == "cancelled"
    runtime = await _runtime(client, thread)
    assert runtime["pending"] == []
    assert runtime["activeJob"] is None

    # The field is free again: the next message starts a new turn instead of being stuck.
    again = await _post(client, thread, "Тогда не надо")
    assert again.status_code == 202, again.text
    assert again.json()["jobId"] != job_id


# --- attachments --------------------------------------------------------------------------------


async def test_chat_v4_sees_the_text_of_the_current_message_files(env: Env, tmp_path: Path) -> None:
    from tests.sources.test_documents import make_docx

    env.settings.agent_engine = "v4"
    client, _user, thread, _space = await _student(env)
    uploaded = await client.post(
        "/api/v1/attachments",
        files={
            "file": (
                "правила.docx",
                make_docx(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    attachment_id = uploaded.json()["id"]

    async def respond(request: LlmRequest) -> LlmResponse:
        return answer("Заявление на пересдачу подаётся за три дня до экзамена.")

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "Что с пересдачей?", attachmentIds=[attachment_id])
    assert sent.status_code == 202, sent.text
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    assert job.result["stopped"] == "answer"

    first = provider.requests[0]
    context_text = "\n".join(
        message.content for message in first.messages if message.role == "user"
    )
    assert "Вложения текущего сообщения студента" in context_text
    assert "Заявление подаётся за три дня до экзамена" in context_text
    # A file's words are data (wrapped), never instructions typed by the student.
    assert '<source_data tool="attachment">' in context_text
    assert job.result["context"]["documents"] > 0


# --- masking of a primary secret inside a chat turn ---------------------------------------------


async def test_chat_v4_masks_a_broker_secret_out_of_messages_and_trace(
    env: Env, tmp_path: Path
) -> None:
    env.settings.agent_engine = "v4"
    client, user, thread, _space = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:
        step = _steps(request)
        if step == 0:
            return tool_response(call("secret_request", {"question": "Пароль от кабинета?"}, "c1"))
        if step == 1:
            return tool_response(
                call(
                    "code_exec",
                    {"language": "python", "code": f"print('profile {SECRET}')"},
                    "c2",
                )
            )
        return answer("Вошёл в кабинет.")

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "Зайди в кабинет")
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))

    await env.wait_for(job_id, {"waiting_user"}, within=30)
    secret_blocks = [
        event.data
        for event in await env.events(job_id)
        if event.type == "block" and event.data.get("type") == "input"
    ]
    assert secret_blocks
    assert secret_blocks[-1]["secret"] is True
    # The protected field of the park turn is visible in the thread runtime: the UI renders the
    # secret input above the composer (a question of the agent turn, not only of a background task).
    runtime = await _runtime(client, thread)
    assert runtime["pending"] == [
        {"jobId": job_id, "blockId": secret_blocks[-1]["id"], "channel": "native", "secret": True}
    ]
    answered = await client.post(
        f"/api/v1/jobs/{job_id}/answers",
        json={"blockId": secret_blocks[-1]["id"], "values": SECRET},
    )
    assert answered.status_code == 204, answered.text

    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None

    # The value is nowhere it could leak: the tool output the model reads is masked (the parser
    # printed the secret and got ***), and neither the thread, the job events, the job result nor
    # the durable trace contain it. (The scripted provider itself wrote the value into the tool
    # arguments, which a real model cannot do: it never sees the value.)
    tool_text = "\n".join(
        message.content
        for request in provider.requests
        for message in request.messages
        if message.role == "tool"
    )
    assert SECRET not in tool_text
    assert "***" in tool_text
    stored = await _last_message(env, thread)
    assert stored is not None
    assert SECRET not in _dumped(stored.blocks)
    assert SECRET not in _dumped([event.data for event in await env.events(job_id)])
    assert SECRET not in _dumped(job.result)
    accepted = await env.answers(job_id)
    assert len(accepted) == 1
    assert accepted[0].is_secret is True
    assert accepted[0].values is None
    async with env.sm() as session:
        payloads = list(
            await session.scalars(
                select(AgentRunEvent.payload)
                .join(AgentRun, AgentRun.run_id == AgentRunEvent.run_id)
                .where(AgentRun.user_id == user)
            )
        )
    assert payloads, "the chat run must have a durable trace"
    assert SECRET not in _dumped(payloads)


# --- API parity and failures --------------------------------------------------------------------


async def test_chat_v4_api_keeps_the_v3_message_semantics(env: Env) -> None:
    env.settings.agent_engine = "v4"
    client, _user, thread, _space = await _student(env)

    empty = await _post(client, thread, "   ")
    assert empty.status_code == 422

    client_message_id = "01M68Z000000000000000000C0"
    one = await _post(client, thread, "привет", clientMessageId=client_message_id)
    assert one.status_code == 202, one.text
    replay = await _post(client, thread, "привет", clientMessageId=client_message_id)
    assert replay.status_code == 202
    assert replay.json()["jobId"] == one.json()["jobId"]
    assert replay.json()["message"]["id"] == one.json()["message"]["id"]

    # One agent turn at a time is still the rule (the queued first turn blocks the second).
    busy = await _post(client, thread, "ещё вопрос")
    assert busy.status_code == 409
    assert busy.json()["code"] == ErrorCode.AGENT_BUSY.value


async def test_chat_v4_provider_failure_becomes_a_honest_answer(env: Env, tmp_path: Path) -> None:
    env.settings.agent_engine = "v4"
    client, _user, thread, _space = await _student(env)

    class FailingProvider:
        name = "failing"

        async def complete(self, request: LlmRequest) -> LlmResponse:
            raise RuntimeError("provider is down")

        async def aclose(self) -> None:
            return None

    sent = await _post(client, thread, "есть новости?")
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, FailingProvider(), tmp_path=tmp_path))
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert job.result is not None
    # Like v3 chat: the student gets a human line in the thread, the job records `stopped=error`.
    assert job.result["stopped"] == "error"
    reply = await _last_message(env, thread)
    assert reply is not None
    assert reply.blocks[0]["markdown"]
