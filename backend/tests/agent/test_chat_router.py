"""Chat HTTP API: threads, messages, tenant isolation (L1, real database)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent import conversations
from botai.agent.chat import handle_chat
from botai.contracts import schemas
from botai.contracts.agent_ui import TextBlock
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext, JobKind
from botai.db.models import ChatMessage, Job
from botai.db.session import db_session
from botai.main import create_app
from tests.agent.conftest import Api, add_user
from tests.agent.seed import add_space
from tests.agent.support import NOW, add_thread

Sm = async_sessionmaker[AsyncSession]


async def chat_jobs(api: Api) -> list[Job]:
    async with api.sm() as session:
        rows = await session.execute(select(Job).where(Job.kind == JobKind.CHAT.value))
        return list(rows.scalars())


async def test_the_chat_requires_a_session(sm: Sm) -> None:
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    app.dependency_overrides[db_session] = _db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        for method, path in (
            ("GET", "/api/v1/chat/threads"),
            ("POST", "/api/v1/chat/threads"),
            ("GET", f"/api/v1/chat/threads/{new_id()}/messages"),
        ):
            response = await client.request(method, path, json={} if method == "POST" else None)
            assert response.status_code == 401, path


async def test_threads_are_created_listed_and_scoped_to_the_student(api: Api) -> None:
    other = await add_user(api.sm, "other")
    my_space = await add_space(api.sm, api.user_id, "Мой вуз")
    foreign_space = await add_space(api.sm, other, "Чужой вуз")
    await add_thread(api.sm, other)

    async with api.client() as client:
        created = await client.post(
            "/api/v1/chat/threads", json={"spaceId": my_space, "title": "Расписание"}
        )
        assert created.status_code == 201
        body = created.json()
        assert (body["spaceId"], body["title"]) == (my_space, "Расписание")

        default = await client.post("/api/v1/chat/threads", json={})
        assert default.json()["title"] == "Новый чат"

        bad_space = await client.post("/api/v1/chat/threads", json={"spaceId": foreign_space})
        assert bad_space.status_code == 404

        listed = (await client.get("/api/v1/chat/threads")).json()
        assert {t["id"] for t in listed} == {body["id"], default.json()["id"]}


async def test_posting_a_message_stores_it_and_queues_one_chat_job(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    async with api.client() as client:
        response = await client.post(
            f"/api/v1/chat/threads/{thread}/messages", json={"text": "Что завтра?"}
        )
        assert response.status_code == 202
        accepted = response.json()
        message = accepted["message"]
        assert message["role"] == "user"
        assert message["blocks"][0]["type"] == "text"
        assert message["blocks"][0]["markdown"] == "Что завтра?"
        assert schemas.errors("agent-ui.schema.json", message["blocks"][0]) == []

        (job,) = await chat_jobs(api)
        assert job.kind == JobKind.CHAT
        assert accepted["jobId"] == job.id
        assert (job.user_id, job.thread_id) == (api.user_id, thread)
        assert job.payload == {"thread_id": thread, "message_id": message["id"]}

        listed = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
        assert [m["id"] for m in listed] == [message["id"]]
        assert schemas.errors("agent-ui.schema.json", listed[0]["blocks"][0]) == []


async def test_the_reply_of_the_job_shows_up_in_the_thread(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    async with api.client() as client:
        accepted = (
            await client.post(f"/api/v1/chat/threads/{thread}/messages", json={"text": "Привет"})
        ).json()
        (job,) = await chat_jobs(api)
        ctx = InMemoryJobContext(user_id=api.user_id, job_id=job.id)
        await handle_chat(ctx, api.services, job.payload)

        listed = (await client.get(f"/api/v1/chat/threads/{thread}/messages")).json()
    assert [m["role"] for m in listed] == ["user", "agent"]
    assert listed[1]["jobId"] == accepted["jobId"]
    assert listed[1]["blocks"][0]["markdown"] == "Ответ."
    assert "sources" not in listed[1]["blocks"][0]
    # The history after a reload must obey the same wire schema as a live SSE block.
    assert schemas.errors("agent-ui.schema.json", listed[1]["blocks"][0]) == []


async def test_legacy_null_optionals_are_normalized_without_losing_unknown_blocks(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    legacy = TextBlock(id=new_id(), at=NOW, markdown="Старый ответ.").model_dump(
        by_alias=True, mode="json"
    )
    assert legacy["sources"] is None
    unknown = {"type": "future", "id": new_id(), "value": None}
    malformed = {"type": "input", "id": new_id(), "question": "Без обязательных полей"}
    message_id = new_id()
    async with api.sm() as session:
        session.add(
            ChatMessage(
                id=message_id,
                user_id=api.user_id,
                thread_id=thread,
                role="agent",
                blocks=[legacy, unknown, malformed],
                created_at=NOW,
            )
        )
        await session.commit()

    async with api.client() as client:
        response = await client.get(f"/api/v1/chat/threads/{thread}/messages")
    assert response.status_code == 200
    [message] = response.json()
    blocks = message["blocks"]
    assert "sources" not in blocks[0]
    assert schemas.errors("agent-ui.schema.json", blocks[0]) == []
    assert blocks[1:] == [unknown, malformed]
    # Compatibility is applied at the boundary, without rewriting the stored history.
    async with api.sm() as session:
        stored = await session.get(ChatMessage, message_id)
        assert stored is not None
        assert stored.blocks == [legacy, unknown, malformed]


async def test_a_second_message_waits_while_the_first_is_being_answered(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    async with api.sm() as session:
        session.add(
            Job(
                user_id=api.user_id,
                kind="chat",
                state="running",
                thread_id=thread,
                run_after=NOW,
            )
        )
        await session.commit()
    async with api.client() as client:
        response = await client.post(
            f"/api/v1/chat/threads/{thread}/messages", json={"text": "Ещё вопрос"}
        )
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert [job.state for job in await chat_jobs(api)] == ["running"]  # only the seeded one
    async with api.sm() as session:
        assert (await session.execute(select(ChatMessage))).scalars().all() == []


async def test_a_message_without_a_job_is_not_left_behind(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = await add_thread(api.sm, api.user_id)

    async def boom(*args: Any, **kwargs: Any) -> Job:
        raise RuntimeError("queue is down")

    monkeypatch.setattr(conversations, "enqueue_in_session", boom)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app, raise_app_exceptions=False),
        base_url="https://testserver",
    ) as client:
        response = await client.post(
            f"/api/v1/chat/threads/{thread}/messages", json={"text": "Вопрос"}
        )
    assert response.status_code == 500
    async with api.sm() as session:
        assert (await session.execute(select(ChatMessage))).scalars().all() == []


async def test_a_foreign_thread_is_invisible(api: Api) -> None:
    other = await add_user(api.sm, "other")
    theirs = await add_thread(api.sm, other)
    async with api.client() as client:
        assert (await client.get(f"/api/v1/chat/threads/{theirs}/messages")).status_code == 404
        post = await client.post(f"/api/v1/chat/threads/{theirs}/messages", json={"text": "эй"})
        assert post.status_code == 404
        assert (await client.get(f"/api/v1/chat/threads/{new_id()}/messages")).status_code == 404
    assert await chat_jobs(api) == []


@pytest.mark.parametrize("text", ["", "я" * 8001])
async def test_message_text_is_validated(api: Api, text: str) -> None:
    thread = await add_thread(api.sm, api.user_id)
    async with api.client() as client:
        response = await client.post(f"/api/v1/chat/threads/{thread}/messages", json={"text": text})
    assert response.status_code == 422
