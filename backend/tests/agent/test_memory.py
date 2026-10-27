"""Thread memory: recent turns whole, older ones summarised into chat_threads.summary."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.memory import (
    RECENT_TOKENS,
    Turn,
    load_turns,
    pack_summary,
    parse_summary,
    refresh_summary,
    split_recent,
    summary_message,
    turn_from_blocks,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.llm import LlmRequest, LlmResponse, ScriptedLlm
from botai.db.models import ChatThread
from tests.agent.conftest import add_user
from tests.agent.support import add_message, add_thread, make_services


def turn(i: int, chars: int = 300, role: str = "user") -> Turn:
    return Turn(f"{i:026d}", "user" if role == "user" else "assistant", "я" * chars)


def test_recent_turns_fit_the_budget_and_the_rest_is_older() -> None:
    turns = [turn(i, 3000) for i in range(10)]  # ~1000 tokens each
    older, recent = split_recent(turns, budget=3500)
    assert [t.id for t in recent] == [t.id for t in turns[-3:]]
    assert [t.id for t in older] == [t.id for t in turns[:-3]]


def test_everything_is_recent_when_it_fits_into_24k() -> None:
    turns = [turn(i) for i in range(20)]
    older, recent = split_recent(turns)
    assert (older, len(recent), RECENT_TOKENS) == ([], 20, 24_000)


def test_the_last_turn_is_kept_even_if_it_alone_exceeds_the_budget() -> None:
    turns = [turn(1), turn(2, 90_000)]
    older, recent = split_recent(turns, budget=1000)
    assert [t.id for t in recent] == [turns[1].id]
    assert [t.id for t in older] == [turns[0].id]


def test_turns_come_from_text_blocks_only() -> None:
    blocks = [
        {"type": "thinking", "stage": "Ищу"},
        {"type": "text", "markdown": "Ответ"},
        {"type": "approval", "proposal": {}},
    ]
    t = turn_from_blocks("M1", "agent", blocks)
    assert t == Turn("M1", "assistant", "Ответ")
    assert turn_from_blocks("M2", "agent", [{"type": "thinking", "stage": "x"}]) is None


def test_summary_marker_round_trip() -> None:
    packed = pack_summary("0" * 26, "- обсуждали дедлайны")
    assert parse_summary(packed) == ("0" * 26, "- обсуждали дедлайны")
    assert parse_summary("старая сводка без метки") == (None, "старая сводка без метки")
    assert parse_summary(None) == (None, "")
    message = summary_message("текст")
    assert message is not None
    assert message.role == "user"
    assert "не данные вуза" in message.content
    assert summary_message("") is None


async def test_load_turns_is_tenant_scoped_and_ordered(
    sm: async_sessionmaker[AsyncSession], user_id: str
) -> None:
    other = await add_user(sm, "other")
    thread = await add_thread(sm, user_id)
    first = await add_message(sm, user_id, thread, "user", "вопрос")
    second = await add_message(sm, user_id, thread, "agent", "ответ")
    services = make_services(ScriptedLlm(), sm=sm)

    turns, summary = await load_turns(services, user_id, thread)
    assert [t.id for t in turns] == [first, second]
    assert [t.role for t in turns] == ["user", "assistant"]
    assert summary is None
    assert await load_turns(services, other, thread) == ([], None)  # not their thread


async def test_older_turns_are_summarised_once_and_stored_with_a_marker(
    sm: async_sessionmaker[AsyncSession], user_id: str
) -> None:
    thread = await add_thread(sm, user_id)
    ids = [await add_message(sm, user_id, thread, "user", f"реплика {i}") for i in range(4)]
    llm = ScriptedLlm(queue=[LlmResponse(content="- обсуждали реплики 0-1")])
    services = make_services(llm, sm=sm)
    turns, stored = await load_turns(services, user_id, thread)
    older, _ = turns[:2], turns[2:]

    text = await refresh_summary(
        services, user_id=user_id, thread_id=thread, older=older, stored=stored
    )
    assert text == "- обсуждали реплики 0-1"
    request = llm.requests[0]
    assert request.purpose == "summarize"
    assert "реплика 0" in request.messages[-1].content
    assert "реплика 3" not in request.messages[-1].content  # recent turns are not summarised

    async with sm() as session:
        row = (
            await session.execute(select(ChatThread).where(ChatThread.id == thread))
        ).scalar_one()
    assert row.summary == pack_summary(ids[1], text)

    # Same older turns again: nothing new, no model call (ScriptedLlm would raise).
    again = await refresh_summary(
        services, user_id=user_id, thread_id=thread, older=older, stored=row.summary
    )
    assert again == text
    assert len(llm.requests) == 1

    # One more turn became older: only it and the previous summary go to the model.
    llm2 = ScriptedLlm(queue=[LlmResponse(content="- обновлено")])
    services2 = make_services(llm2, sm=sm)
    await refresh_summary(
        services2, user_id=user_id, thread_id=thread, older=turns[:3], stored=row.summary
    )
    prompt = llm2.requests[0].messages[-1].content
    assert "обсуждали реплики 0-1" in prompt
    assert "реплика 2" in prompt
    assert "реплика 0" not in prompt


async def test_a_failing_summary_keeps_the_old_one(
    sm: async_sessionmaker[AsyncSession], user_id: str
) -> None:
    class Down:
        async def complete(self, request: LlmRequest) -> LlmResponse:
            raise BotaiError(ErrorCode.LLM_UNAVAILABLE, "недоступно", status=503)

    thread = await add_thread(sm, user_id)
    await add_message(sm, user_id, thread, "user", "раз")
    services = make_services(Down(), sm=sm)  # type: ignore[arg-type]
    turns, _ = await load_turns(services, user_id, thread)
    stored = pack_summary(new_id(), "прежняя сводка")
    text = await refresh_summary(
        services, user_id=user_id, thread_id=thread, older=turns, stored=stored
    )
    assert text == "прежняя сводка"
