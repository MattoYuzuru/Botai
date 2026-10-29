"""The thread list: unread, attention, active task, preview; rename, read, delete (L1)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from botai.core.ids import new_id
from botai.db.models import ChatMessage, ChatThread, Connection, Job
from tests.agent.conftest import Api, add_user
from tests.agent.seed import add_connection, add_space
from tests.agent.support import NOW, add_thread

T = NOW  # the frozen clock of the API fixture


async def add_agent_message(
    api: Api,
    thread: str,
    text: str = "Ответ",
    *,
    at: Any = T,
    block: dict[str, Any] | None = None,
    role: str = "agent",
) -> None:
    async with api.sm() as session:
        session.add(
            ChatMessage(
                user_id=api.user_id,
                thread_id=thread,
                role=role,
                blocks=[
                    block
                    or {"type": "text", "id": new_id(), "at": at.isoformat(), "markdown": text}
                ],
                created_at=at,
            )
        )
        await session.commit()


async def add_job(api: Api, thread: str, kind: str, state: str, **extra: Any) -> str:
    job_id = new_id()
    async with api.sm() as session:
        session.add(
            Job(
                id=job_id,
                user_id=api.user_id,
                kind=kind,
                state=state,
                thread_id=thread,
                run_after=T,
                **{"created_at": T, **extra},
            )
        )
        await session.commit()
    return job_id


async def listing(api: Api) -> dict[str, dict[str, Any]]:
    async with api.client() as client:
        response = await client.get("/api/v1/chat/threads")
    assert response.status_code == 200
    return {t["id"]: t for t in response.json()}


async def test_every_thread_is_listed_newest_first_and_only_the_students_own(api: Api) -> None:
    other = await add_user(api.sm, "other")
    await add_thread(api.sm, other)
    old, new = await add_thread(api.sm, api.user_id), await add_thread(api.sm, api.user_id)
    await add_agent_message(api, old, at=T + timedelta(hours=1))
    await add_agent_message(api, new, at=T + timedelta(hours=2))
    async with api.client() as client:
        body = (await client.get("/api/v1/chat/threads")).json()
    assert [t["id"] for t in body[:2]] == [new, old]
    assert len(body) == 2
    assert set(body[0]) == {
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


async def test_a_message_moves_last_message_at_whoever_inserted_it(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    later = T + timedelta(days=3)
    await add_agent_message(api, thread, at=later)
    await add_agent_message(api, thread, "Старое", at=T - timedelta(days=3))  # never moves it back
    async with api.sm() as session:
        row = await session.get(ChatThread, thread)
    assert row is not None
    assert row.last_message_at == later


async def test_a_new_agent_message_is_unread_until_the_thread_is_opened(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    assert (await listing(api))[thread]["unread"] is False  # nothing written yet
    await add_agent_message(api, thread, at=T - timedelta(hours=1))
    assert (await listing(api))[thread]["unread"] is True
    async with api.client() as client:
        assert (await client.post(f"/api/v1/chat/threads/{thread}/read")).status_code == 204
    assert (await listing(api))[thread]["unread"] is False
    await add_agent_message(api, thread, role="user", at=T + timedelta(hours=2))
    assert (await listing(api))[thread]["unread"] is False  # the student's own words are read
    await add_agent_message(
        api,
        thread,
        block={"type": "thinking", "id": new_id(), "at": T.isoformat(), "stage": "Смотрю"},
        at=T + timedelta(hours=3),
    )
    assert (await listing(api))[thread]["unread"] is False  # progress lines are not messages
    await add_agent_message(api, thread, at=T + timedelta(hours=4))
    assert (await listing(api))[thread]["unread"] is True


async def test_attention_follows_the_running_work_of_the_thread(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    assert (await listing(api))[thread]["attention"] is None
    chat = await add_job(api, thread, "chat", "running")
    assert (await listing(api))[thread]["attention"] == "running"
    task = await add_job(api, thread, "connect", "waiting_user")
    assert (await listing(api))[thread]["attention"] == "waiting_user"
    async with api.sm() as session:
        for job_id in (chat, task):
            row = await session.get(Job, job_id)
            assert row is not None
            row.state = "done"
        await session.commit()
    assert (await listing(api))[thread]["attention"] is None


async def test_a_failed_task_calls_for_attention_until_the_thread_is_opened(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    await add_job(
        api,
        thread,
        "repair",
        "failed",
        finished_at=T - timedelta(minutes=5),
        error={"code": "x", "detail": "y", "status": 500},
    )
    assert (await listing(api))[thread]["attention"] == "failed"
    async with api.client() as client:
        await client.post(f"/api/v1/chat/threads/{thread}/read")
    assert (await listing(api))[thread]["attention"] is None
    # a newer successful job replaces the failure
    await add_job(
        api,
        thread,
        "repair",
        "failed",
        created_at=T + timedelta(hours=8),
        finished_at=T + timedelta(hours=9),
    )
    assert (await listing(api))[thread]["attention"] == "failed"
    async with api.sm() as session:
        session.add(
            Job(
                user_id=api.user_id,
                kind="chat",
                state="done",
                thread_id=thread,
                run_after=T,
                created_at=T + timedelta(days=1),
            )
        )
        await session.commit()
    assert (await listing(api))[thread]["attention"] is None


async def test_the_active_task_names_the_source_and_the_state(api: Api) -> None:
    space = await add_space(api.sm, api.user_id)
    source = await add_connection(api.sm, api.user_id, space, "Smart LMS")
    thread = await add_thread(api.sm, api.user_id)
    quiet = await add_thread(api.sm, api.user_id)
    job = await add_job(api, thread, "connect", "waiting_user", connection_id=source)
    out = await listing(api)
    assert out[thread]["activeTask"] == {
        "jobId": job,
        "kind": "connect",
        "connectionId": source,
        "state": "waiting_user",
        "title": "Подключение: Smart LMS",
    }
    assert out[quiet]["activeTask"] is None
    # a plain agent turn is attention, but not a task card
    await add_job(api, quiet, "chat", "running")
    out = await listing(api)
    assert out[quiet]["activeTask"] is None
    assert out[quiet]["attention"] == "running"


async def test_the_preview_is_the_start_of_the_last_text_message(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    assert (await listing(api))[thread]["preview"] is None
    await add_agent_message(api, thread, "Первое", at=T + timedelta(minutes=1))
    await add_agent_message(
        api, thread, "Слово   " * 40 + "\nконец", at=T + timedelta(minutes=2), role="user"
    )
    await add_agent_message(
        api,
        thread,
        block={"type": "plan", "id": new_id(), "at": T.isoformat(), "title": "План", "steps": []},
        at=T + timedelta(minutes=3),
    )  # not a text message: skipped
    preview = (await listing(api))[thread]["preview"]
    assert preview is not None
    assert len(preview) <= 120
    assert preview.startswith("Слово Слово")
    assert preview.endswith("…")
    assert "\n" not in preview


async def test_a_thread_is_renamed_and_a_foreign_one_is_not_found(api: Api) -> None:
    other = await add_user(api.sm, "other")
    theirs = await add_thread(api.sm, other)
    mine = await add_thread(api.sm, api.user_id)
    async with api.client() as client:
        renamed = await client.patch(
            f"/api/v1/chat/threads/{mine}", json={"title": "  Дедлайны   по ВШЭ "}
        )
        assert renamed.status_code == 200
        assert renamed.json()["title"] == "Дедлайны по ВШЭ"
        assert (
            await client.patch(f"/api/v1/chat/threads/{mine}", json={"title": ""})
        ).status_code == 422
        assert (
            await client.patch(f"/api/v1/chat/threads/{theirs}", json={"title": "x"})
        ).status_code == 404
        assert (await client.post(f"/api/v1/chat/threads/{theirs}/read")).status_code == 404
        assert (await client.delete(f"/api/v1/chat/threads/{theirs}")).status_code == 404
    async with api.sm() as session:
        assert (await session.get(ChatThread, theirs)) is not None


async def test_deleting_a_thread_stops_its_tasks_and_removes_its_messages(api: Api) -> None:
    space = await add_space(api.sm, api.user_id)
    source = await add_connection(api.sm, api.user_id, space, "Smart LMS", status="learning")
    thread = await add_thread(api.sm, api.user_id)
    survivor = await add_thread(api.sm, api.user_id)
    await add_agent_message(api, thread, "Привет")
    task = await add_job(api, thread, "connect", "running", connection_id=source)
    chat = await add_job(api, thread, "chat", "queued")
    done = await add_job(api, thread, "sync", "done", connection_id=source)
    elsewhere = await add_connection(api.sm, api.user_id, space, "Другой источник")
    kept = await add_job(api, survivor, "sync", "running", connection_id=elsewhere)
    async with api.client() as client:
        assert (await client.delete(f"/api/v1/chat/threads/{thread}")).status_code == 204
        assert (await client.get(f"/api/v1/chat/threads/{thread}/messages")).status_code == 404
    async with api.sm() as session:
        assert await session.get(ChatThread, thread) is None
        assert (await session.get(ChatThread, survivor)) is not None
        states = {j.id: (j.state, j.thread_id) for j in await session.scalars(select(Job))}
        assert (
            await session.scalar(select(ChatMessage.id).where(ChatMessage.thread_id == thread))
        ) is None
    assert states[task] == ("cancelled", None)
    assert states[chat] == ("cancelled", None)
    assert states[done] == ("done", None)
    assert states[kept] == ("running", survivor)  # another thread's task is left alone
    async with api.sm() as session:
        row = await session.get(Connection, source)
        assert row is not None
        assert row.status == "draft"  # a stopped setup is restored, not left "learning"
