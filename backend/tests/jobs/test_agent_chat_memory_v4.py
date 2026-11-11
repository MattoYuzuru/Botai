"""v4 CHAT leaves something for the next chats (docs/v4/26-chat-memory.md), through the real worker.

The audit of 2026-11-05: a long production conversation about a coursework application left no
note, no summary and nothing searchable. These tests drive the product path (API → worker → v4
harness with a scripted model) and check what a turn now leaves behind: the note the agent kept
(and the line the student sees about it), the rolling thread summary from the cheap `summarize`
call, and that a later chat in another thread starts with that memory and the tools to dig deeper.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from botai.agent.memory import parse_summary
from botai.agent.v4.chat import refresh_thread_summary
from botai.core.ids import new_id
from botai.core.llm import LlmRequest, LlmResponse, ScriptedLlm
from botai.db.models import ChatMessage, ChatThread, UserMemory
from tests.agent.v4.runtime_support import answer, call, tool_response
from tests.auth.support import NOW
from tests.jobs.env import Env
from tests.jobs.test_agent_chat_v4 import (
    FlowProvider,
    _chat_worker,
    _last_message,
    _post,
    _steps,
    _student,
)

pytestmark = pytest.mark.db

NOTE = "Научный руководитель курсовой — доцент Петров, тема про графовые нейросети"
SUMMARY = "- Студент подаёт заявку на курсовую в Smart LMS; руководитель — доцент Петров."


def _text(request: LlmRequest) -> str:
    return "\n".join(message.content for message in request.messages)


async def _new_thread(client: Any, space_id: str) -> str:
    created = await client.post("/api/v1/chat/threads", json={"spaceId": space_id})
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def test_a_turn_keeps_a_note_and_a_summary_that_the_next_chat_starts_with(
    env: Env, tmp_path: Path
) -> None:
    summarizer = ScriptedLlm(queue=[LlmResponse(content=SUMMARY)])
    env.services.llm = summarizer
    client, user, thread, space = await _student(env, with_space=True)
    assert space is not None

    async def respond(request: LlmRequest) -> LlmResponse:
        if "Мой руководитель" in _text(request) and _steps(request) == 0:
            return tool_response(call("memory_remember", {"note": NOTE}, "m1"))
        if "Мой руководитель" in _text(request):
            return answer("Хорошо, учту при заполнении заявки.")
        return answer("Ваш руководитель — доцент Петров.")

    provider = FlowProvider(respond)
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))

    sent = await _post(
        client, thread, "Мой руководитель — доцент Петров, тема про графовые нейросети."
    )
    job = await env.wait_for(sent.json()["jobId"], {"done"}, within=30)
    assert job.result is not None
    assert job.result["stopped"] == "answer"

    # The system prompt says when to remember and to ingest, and gives the chat's space id.
    first = provider.requests[0]
    names = {tool.name for tool in first.tools}
    assert {"memory_remember", "memory_forget", "threads_search", "threads_read"} <= names
    prompt = _text(first)
    assert "Память между чатами" in prompt
    assert "rag_ingest" in prompt
    assert f"space_id={space} — к нему относится этот чат" in prompt

    # The note is in the student's memory, and the student is told what was kept.
    async with env.sm() as session:
        notes = list(
            await session.scalars(select(UserMemory.note).where(UserMemory.user_id == user))
        )
    assert notes == [NOTE]
    reply = await _last_message(env, thread)
    assert reply is not None
    assert reply.blocks[0]["markdown"].endswith(f"Запомнил: «{NOTE}».")

    # The rolling summary was refreshed by one cheap call over this turn.
    (summarize,) = summarizer.requests
    assert summarize.purpose == "summarize"
    assert "доцент Петров" in _text(summarize)
    async with env.sm() as session:
        row = await session.get(ChatThread, thread)
    assert row is not None
    covered, text = parse_summary(row.summary)
    assert text == SUMMARY
    assert covered == reply.id, "the summary covers the thread up to the stored reply"

    # A later chat in another thread starts with the note in its context. Here the summarizer has
    # no answer left: the reply is still stored, the old summary of that thread stays empty.
    other = await _new_thread(client, space)
    again = await _post(client, other, "Кто у меня руководитель?")
    job = await env.wait_for(again.json()["jobId"], {"done"}, within=30)
    assert job.result is not None
    assert job.result["stopped"] == "answer"
    later = provider.requests[-1]
    assert NOTE in _text(later)
    assert {"threads_search", "threads_read"} <= {tool.name for tool in later.tools}
    stored = await _last_message(env, other)
    assert stored is not None
    assert stored.blocks[0]["markdown"] == "Ваш руководитель — доцент Петров."
    async with env.sm() as session:
        row = await session.get(ChatThread, other)
    assert row is not None
    assert row.summary is None


async def test_a_short_thread_is_not_read_twice_with_its_own_summary(
    env: Env, tmp_path: Path
) -> None:
    env.services.llm = ScriptedLlm(
        queue=[LlmResponse(content=SUMMARY), LlmResponse(content=SUMMARY + " Ещё вопрос.")]
    )
    client, _user, thread, _space = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:
        del request
        return answer("Готово.")

    provider = FlowProvider(respond)
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))
    for text in ("Первый вопрос про курсовую", "Второй вопрос про курсовую"):
        sent = await _post(client, thread, text)
        await env.wait_for(sent.json()["jobId"], {"done"}, within=30)

    # Both turns fit the recent window: the stored summary is not put in front of them again.
    second = _text(provider.requests[-1])
    assert "Первый вопрос про курсовую" in second
    assert "Сводка прежней переписки" not in second
    async with env.sm() as session:
        row = await session.get(ChatThread, thread)
    assert row is not None
    assert parse_summary(row.summary)[1] == SUMMARY + " Ещё вопрос."


async def test_a_long_unsummarised_thread_is_folded_from_its_newest_turns(env: Env) -> None:
    summarizer = ScriptedLlm(queue=[LlmResponse(content=SUMMARY)])
    env.services.llm = summarizer
    _client, user, thread, _space = await _student(env)
    filler = "текст " * 2_000  # a few thousand tokens per turn: the thread does not fit one call
    async with env.sm() as session:
        for index in range(30):
            session.add(
                ChatMessage(
                    id=new_id(),
                    user_id=user,
                    thread_id=thread,
                    role="user" if index % 2 == 0 else "agent",
                    blocks=[
                        {
                            "type": "text",
                            "id": new_id(),
                            "at": NOW.isoformat(),
                            "markdown": f"Реплика {index:02d}. {filler}",
                        }
                    ],
                    created_at=NOW + timedelta(minutes=index),
                )
            )
        await session.commit()

    await refresh_thread_summary(env.services, user, thread)

    (request,) = summarizer.requests
    prompt = _text(request)
    assert "Реплика 29." in prompt
    assert "Реплика 00." not in prompt
    async with env.sm() as session:
        row = await session.get(ChatThread, thread)
    assert row is not None
    assert parse_summary(row.summary)[1] == SUMMARY


async def test_a_rule_is_kept_shown_as_a_rule_and_reaches_the_next_chat_as_its_own_block(
    env: Env, tmp_path: Path
) -> None:
    env.services.llm = ScriptedLlm(queue=[LlmResponse(content=SUMMARY)])
    client, user, thread, space = await _student(env, with_space=True)
    assert space is not None
    rule = "Про оценки сначала смотри ЦУ, потом ВШЭ"

    async def respond(request: LlmRequest) -> LlmResponse:
        if "Запомни правило" in _text(request) and _steps(request) == 0:
            return tool_response(call("memory_remember", {"note": rule, "kind": "rule"}, "r1"))
        return answer("Хорошо.")

    provider = FlowProvider(respond)
    env.start(await _chat_worker(env, provider, tmp_path=tmp_path))
    sent = await _post(client, thread, "Запомни правило: про оценки сначала смотри ЦУ.")
    await env.wait_for(sent.json()["jobId"], {"done"}, within=30)

    reply = await _last_message(env, thread)
    assert reply is not None
    assert reply.blocks[0]["markdown"].endswith(f"Запомнил правило: «{rule}».")
    async with env.sm() as session:
        kinds = list(
            await session.scalars(select(UserMemory.kind).where(UserMemory.user_id == user))
        )
    assert kinds == ["rule"]
    listed = (await client.get("/api/v1/me/memory")).json()
    assert [(item["note"], item["kind"]) for item in listed] == [(rule, "rule")]

    other = await _new_thread(client, space)
    again = await _post(client, other, "Какие у меня оценки?")
    await env.wait_for(again.json()["jobId"], {"done"}, within=30)
    later = _text(provider.requests[-1])
    assert "Правила студента (его собственные постоянные указания" in later
    assert rule in later
    assert "Память о студенте (заметки" not in later, "a rule is not presented as a note"
