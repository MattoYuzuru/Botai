"""`threads_search` / `threads_read` of the v4 agent (docs/v4/26-chat-memory.md §3), tool level.

Threads and messages are real rows in a Postgres transaction. What matters: the search finds a
conversation by other word forms (Russian stemming, prefixes), by its title and rolling summary,
skips the current chat, never shows another student's chats, and a deleted thread is gone at once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from botai.agent.memory import pack_summary
from botai.agent.v4.protocol import ToolContext, ToolHandler
from botai.agent.v4.tools.threads import ThreadToolkit, build_thread_tools, search_terms
from botai.core.ids import new_id
from botai.db.models import ChatMessage, ChatThread
from tests.auth.support import *  # noqa: F403
from tests.auth.support import Sm
from tests.sources.v4.support import Account, create_account

pytestmark = pytest.mark.db

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


@pytest.fixture(name="account")
async def _account(tx_sessionmaker: Sm) -> Account:
    return await create_account(tx_sessionmaker)


@pytest.fixture(name="other")
async def _other(tx_sessionmaker: Sm) -> Account:
    return await create_account(tx_sessionmaker, title="Другой вуз")


def _tools(sm: Sm) -> dict[str, ToolHandler]:
    return {tool.spec.name: tool for tool in build_thread_tools(ThreadToolkit(sm=sm))}


def _context(account: Account, thread_id: str | None = None) -> ToolContext:
    meta: dict[str, Any] = {"kind": "chat"}
    if thread_id is not None:
        meta["thread_id"] = thread_id
    return ToolContext(run_id="run-1", user_id=account.user_id, metadata=meta)


def _text(markdown: str) -> list[dict[str, Any]]:
    return [{"type": "text", "id": new_id(), "at": T0.isoformat(), "markdown": markdown}]


async def _thread(
    sm: Sm,
    account: Account,
    title: str,
    turns: list[tuple[str, str]],
    *,
    summary: str | None = None,
    start: datetime = T0,
) -> str:
    thread_id = new_id()
    async with sm() as session:
        session.add(
            ChatThread(
                id=thread_id,
                user_id=account.user_id,
                space_id=account.space_id,
                title=title,
                summary=summary,
            )
        )
        await session.flush()
        for index, (role, markdown) in enumerate(turns):
            session.add(
                ChatMessage(
                    id=new_id(),
                    user_id=account.user_id,
                    thread_id=thread_id,
                    role=role,
                    blocks=_text(markdown),
                    created_at=start + timedelta(minutes=index),
                )
            )
            # A thinking line between turns is not conversation and is never a match.
            if role == "user":
                session.add(
                    ChatMessage(
                        id=new_id(),
                        user_id=account.user_id,
                        thread_id=thread_id,
                        role="agent",
                        blocks=[
                            {
                                "type": "thinking",
                                "id": new_id(),
                                "at": T0.isoformat(),
                                "stage": "Думал о руководителе",
                                "state": "ok",
                            }
                        ],
                        created_at=start + timedelta(minutes=index, seconds=30),
                    )
                )
        await session.commit()
    return thread_id


async def _coursework(sm: Sm, account: Account) -> str:
    return await _thread(
        sm,
        account,
        "Заявка на курсовую",
        [
            ("user", "Помоги подать заявку на курсовую работу в Smart LMS."),
            (
                "agent",
                "Заявка подаётся через курс 302976. Нужен научный руководитель и тема; "
                "шаблон заявки я скачал и заполнил.",
            ),
            ("user", "Мой руководитель — доцент Петров, тема про графовые нейросети."),
            ("agent", "Записал: руководитель доцент Петров, тема — графовые нейросети."),
        ],
        start=T0,
    )


def test_search_terms_keep_only_words() -> None:
    assert search_terms("Руководитель & курсовой | ':*' !!") == ["руководитель", "курсовой"]
    assert search_terms("  ") == []


async def test_search_finds_the_conversation_by_other_word_forms(
    tx_sessionmaker: Sm, account: Account
) -> None:
    coursework = await _coursework(tx_sessionmaker, account)
    await _thread(
        tx_sessionmaker,
        account,
        "Расписание",
        [("user", "Какие пары завтра?"), ("agent", "Завтра две пары: матанализ и алгебра.")],
        start=T0 + timedelta(hours=1),
    )
    current = await _thread(
        tx_sessionmaker, account, "Новый чат", [("user", "Кто мой научный руководитель?")]
    )

    result = await _tools(tx_sessionmaker)["threads_search"].run(
        {"query": "научного руководителя курсовой"}, _context(account, current)
    )

    assert result.ok
    threads = result.data["threads"]
    assert [t["threadId"] for t in threads] == [coursework], "current chat and noise are skipped"
    found = threads[0]
    assert found["title"] == "Заявка на курсовую"
    assert found["link"] == f"botai://threads/{coursework}"
    roles = {m["role"] for m in found["matches"]}
    assert roles <= {"student", "assistant"}
    assert any("Петров" in m["excerpt"] for m in found["matches"])
    assert all("Думал" not in m["excerpt"] for m in found["matches"])
    assert len(found["matches"]) <= 3


async def test_search_matches_the_title_and_the_rolling_summary(
    tx_sessionmaker: Sm, account: Account
) -> None:
    summarised = await _thread(
        tx_sessionmaker,
        account,
        "Документы",
        [("user", "Привет"), ("agent", "Здравствуйте!")],
        summary=pack_summary(new_id(), "- Студент оформляет академический отпуск, ждёт приказа."),
    )

    result = await _tools(tx_sessionmaker)["threads_search"].run(
        {"query": "академ отпуск"}, _context(account)
    )

    assert [t["threadId"] for t in result.data["threads"]] == [summarised]
    assert result.data["threads"][0]["summary"].startswith("- Студент оформляет")
    assert "[до сообщения" not in result.data["threads"][0]["summary"]


async def test_search_without_query_lists_recent_chats_and_can_include_the_current(
    tx_sessionmaker: Sm, account: Account
) -> None:
    older = await _thread(tx_sessionmaker, account, "Старый", [("user", "раз")], start=T0)
    newer = await _thread(
        tx_sessionmaker, account, "Новый", [("user", "два")], start=T0 + timedelta(days=1)
    )
    tools = _tools(tx_sessionmaker)

    listed = await tools["threads_search"].run({}, _context(account, newer))
    assert [t["threadId"] for t in listed.data["threads"]] == [older]

    with_current = await tools["threads_search"].run(
        {"include_current": True}, _context(account, newer)
    )
    assert [t["threadId"] for t in with_current.data["threads"]] == [newer, older]


async def test_search_never_shows_another_students_chats(
    tx_sessionmaker: Sm, account: Account, other: Account
) -> None:
    await _coursework(tx_sessionmaker, other)
    tools = _tools(tx_sessionmaker)

    found = await tools["threads_search"].run({"query": "руководитель"}, _context(account))
    listed = await tools["threads_search"].run({}, _context(account))

    assert found.ok
    assert found.data["threads"] == []
    assert listed.data["threads"] == []


async def test_a_deleted_thread_is_gone_from_search_and_read(
    tx_sessionmaker: Sm, account: Account
) -> None:
    coursework = await _coursework(tx_sessionmaker, account)
    tools = _tools(tx_sessionmaker)
    before = await tools["threads_search"].run({"query": "Петров"}, _context(account))
    assert [t["threadId"] for t in before.data["threads"]] == [coursework]

    async with tx_sessionmaker() as session:
        row = await session.get(ChatThread, coursework)
        assert row is not None
        await session.delete(row)
        await session.commit()

    after = await tools["threads_search"].run({"query": "Петров"}, _context(account))
    read = await tools["threads_read"].run({"thread_id": coursework}, _context(account))
    assert after.data["threads"] == []
    assert not read.ok
    assert read.error is not None
    assert read.error.code == "not_found"


async def test_read_returns_the_turns_in_order_and_pages_back(
    tx_sessionmaker: Sm, account: Account, other: Account
) -> None:
    coursework = await _coursework(tx_sessionmaker, account)
    tools = _tools(tx_sessionmaker)

    whole = await tools["threads_read"].run({"thread_id": coursework}, _context(account))
    assert whole.ok
    turns = whole.data["turns"]
    assert [t["role"] for t in turns] == ["student", "assistant", "student", "assistant"]
    assert turns[0]["text"].startswith("Помоги подать заявку")
    assert "Думал" not in whole.to_model_text()

    last_two = await tools["threads_read"].run(
        {"thread_id": coursework, "limit": 2}, _context(account)
    )
    assert [t["text"][:6] for t in last_two.data["turns"]] == ["Мой ру", "Записа"]
    assert last_two.next is not None
    earlier = await tools["threads_read"].run(
        {
            "thread_id": coursework,
            "limit": 2,
            "before_message_id": last_two.data["turns"][0]["messageId"],
        },
        _context(account),
    )
    assert [t["role"] for t in earlier.data["turns"]] == ["student", "assistant"]
    assert earlier.data["turns"][0]["text"].startswith("Помоги")

    foreign = await tools["threads_read"].run({"thread_id": coursework}, _context(other))
    assert not foreign.ok
    assert foreign.error is not None
    assert foreign.error.code == "not_found"
