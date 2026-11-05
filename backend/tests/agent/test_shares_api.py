"""Read-only links to a thread for other BotAI users: open, read, close (L1)."""

from __future__ import annotations

from typing import Any

from botai.core.ids import new_id
from tests.agent.conftest import Api, add_user
from tests.agent.support import NOW, add_thread
from tests.agent.test_threads_api import add_agent_message

T = NOW


def _text(markdown: str, **extra: Any) -> dict[str, Any]:
    return {"type": "text", "id": new_id(), "at": T.isoformat(), "markdown": markdown, **extra}


async def test_the_owner_opens_one_link_per_thread_and_a_friend_reads_only_the_words(
    api: Api,
) -> None:
    thread = await add_thread(api.sm, api.user_id)
    await add_agent_message(
        api,
        thread,
        role="user",
        block=_text("Что нужно для курсовой?", quote={"text": "Какой курс?"}),
    )
    await add_agent_message(
        api, thread, block={"type": "thinking", "id": new_id(), "stage": "Читаю курс"}
    )
    await add_agent_message(api, thread, "**Заявление** и форма №2")
    await add_agent_message(
        api,
        thread,
        role="user",
        block={
            "type": "attachment",
            "id": new_id(),
            "attachmentId": new_id(),
            "name": "шаблон.docx",
            "mime": "application/msword",
            "size": 1200,
            "kind": "document",
        },
    )
    async with api.client() as client:
        assert (await client.get(f"/api/v1/chat/threads/{thread}/share")).json() is None
        first = (await client.put(f"/api/v1/chat/threads/{thread}/share")).json()
        again = (await client.put(f"/api/v1/chat/threads/{thread}/share")).json()
        assert again["shareId"] == first["shareId"]  # sharing twice keeps the link
        assert len(first["shareId"]) >= 22
        assert (await client.get(f"/api/v1/chat/threads/{thread}/share")).json() == first

    friend = await add_user(api.sm, "friend")
    api.login_as(friend)
    async with api.client() as client:
        response = await client.get(f"/api/v1/shared/chats/{first['shareId']}")
    assert response.status_code == 200
    body = response.json()
    assert body["ownerName"] == "student"
    assert body["mine"] is False
    assert body["threadId"] is None  # the friend gets no address of the owner's thread
    messages = body["messages"]
    assert [(m["role"], m["markdown"]) for m in messages] == [
        ("user", "Что нужно для курсовой?"),
        ("agent", "**Заявление** и форма №2"),
        ("user", ""),
    ]  # reasoning steps are not shared
    assert messages[0]["quote"] == "Какой курс?"
    assert messages[2]["attachments"] == [
        {"name": "шаблон.docx", "mime": "application/msword", "size": 1200, "kind": "document"}
    ]  # a file name only: no attachment id to open the owner's file with

    # The friend cannot open, close or re-share the owner's thread itself.
    async with api.client() as client:
        assert (await client.get(f"/api/v1/chat/threads/{thread}/messages")).status_code == 404
        assert (await client.delete(f"/api/v1/chat/threads/{thread}/share")).status_code == 404
        assert (await client.put(f"/api/v1/chat/threads/{thread}/share")).status_code == 404


async def test_the_owner_sees_their_own_link_with_the_thread_address(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    await add_agent_message(api, thread, "Ответ")
    async with api.client() as client:
        share = (await client.put(f"/api/v1/chat/threads/{thread}/share")).json()
        body = (await client.get(f"/api/v1/shared/chats/{share['shareId']}")).json()
    assert body["mine"] is True
    assert body["threadId"] == thread


async def test_closing_the_link_kills_it_and_sharing_again_makes_a_new_one(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    async with api.client() as client:
        old = (await client.put(f"/api/v1/chat/threads/{thread}/share")).json()["shareId"]
        assert (await client.delete(f"/api/v1/chat/threads/{thread}/share")).status_code == 204
        assert (await client.get(f"/api/v1/shared/chats/{old}")).status_code == 404
        assert (await client.get(f"/api/v1/chat/threads/{thread}/share")).json() is None
        new = (await client.put(f"/api/v1/chat/threads/{thread}/share")).json()["shareId"]
        assert new != old
        assert (await client.get(f"/api/v1/shared/chats/{old}")).status_code == 404
        assert (await client.get(f"/api/v1/shared/chats/{new}")).status_code == 200


async def test_a_deleted_thread_takes_its_link_with_it(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    async with api.client() as client:
        share = (await client.put(f"/api/v1/chat/threads/{thread}/share")).json()["shareId"]
        assert (await client.delete(f"/api/v1/chat/threads/{thread}")).status_code == 204
        assert (await client.get(f"/api/v1/shared/chats/{share}")).status_code == 404


async def test_an_unknown_link_is_not_found(api: Api) -> None:
    async with api.client() as client:
        response = await client.get("/api/v1/shared/chats/nope-nope-nope-nope-nope")
    assert response.status_code == 404
