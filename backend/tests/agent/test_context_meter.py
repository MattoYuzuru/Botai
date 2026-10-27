"""GET /chat/threads/{id}/context: the scale of the agent's context (agent-harness.md §6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from botai.agent.chat import handle_chat
from botai.agent.context_meter import CONTEXT_LIMIT, LABELS
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext
from botai.core.llm import LlmResponse, LlmToolCall, LlmUsage, ScriptedLlm
from botai.db.models import Job
from tests.agent.conftest import Api, add_user
from tests.agent.seed import (
    FakeReader,
    add_connection,
    add_document,
    add_space,
    event,
    provenance,
)
from tests.agent.support import NOW, add_message, add_thread

KINDS = ["system", "profile", "data", "documents", "history", "free"]


def tool_step(*calls: LlmToolCall) -> LlmResponse:
    return LlmResponse(tool_calls=list(calls), usage=LlmUsage(input_cache_miss=50, output=5))


async def get_context(api: Api, thread: str) -> httpx.Response:
    async with api.client() as client:
        return await client.get(f"/api/v1/chat/threads/{thread}/context")


async def test_a_new_thread_shows_rules_and_profile_only(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    response = await get_context(api, thread)
    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == CONTEXT_LIMIT
    assert [s["kind"] for s in body["segments"]] == KINDS
    assert [s["label"] for s in body["segments"]] == [LABELS[k] for k in KINDS]
    tokens = {s["kind"]: s["tokens"] for s in body["segments"]}
    assert tokens["system"] > 500  # the rules and ten tool schemas are not free
    assert tokens["profile"] > 0
    assert (tokens["data"], tokens["documents"], tokens["history"]) == (0, 0, 0)
    assert body["used"] == sum(v for k, v in tokens.items() if k != "free")
    assert tokens["free"] == CONTEXT_LIMIT - body["used"]
    assert body["sources"] == []


async def test_after_an_answer_it_shows_data_documents_history_and_sources(api: Api) -> None:
    space = await add_space(api.sm, api.user_id, "Вуз А")
    conn = await add_connection(api.sm, api.user_id, space, "Расписание ЛК")
    await add_document(
        api.sm, api.user_id, space, "Положение", ["Справка выдаётся в офисе за три дня."]
    )
    thread = await add_thread(api.sm, api.user_id, space_id=space)
    qid = await add_message(
        api.sm,
        api.user_id,
        thread,
        "user",
        "Что завтра и как получить справку?",
        at=NOW - timedelta(minutes=1),
    )
    reader = FakeReader(
        events=[event(provenance(conn, space), "Анализ", datetime(2026, 10, 29, 6, 30, tzinfo=UTC))]
    )
    llm = ScriptedLlm(
        queue=[
            tool_step(
                LlmToolCall(id="a", name="get_schedule", arguments={"from_date": "2026-10-29"}),
                LlmToolCall(id="b", name="search_documents", arguments={"query": "справка"}),
            ),
            LlmResponse(content="Завтра анализ [S1]. Справка за три дня [S2]."),
        ]
    )
    api.services.llm = llm
    job_id = new_id()
    out = await handle_chat(
        InMemoryJobContext(user_id=api.user_id, job_id=job_id),
        api.services,
        {"thread_id": thread, "message_id": qid},
        reader=reader,
    )
    async with api.sm() as session:  # what W1's runner does when the handler returns
        session.add(
            Job(
                id=job_id,
                user_id=api.user_id,
                kind="chat",
                state="done",
                thread_id=thread,
                result=out,
                run_after=NOW,
            )
        )
        await session.commit()

    body = (await get_context(api, thread)).json()
    tokens = {s["kind"]: s["tokens"] for s in body["segments"]}
    assert tokens["data"] > 0
    assert tokens["documents"] > 0
    assert tokens["history"] > 0  # the question and the stored answer
    assert [(s["title"], s["kind"]) for s in body["sources"]] == [
        ("Расписание ЛК", "data"),
        ("Положение", "page"),
    ]
    assert body["sources"][0]["fetchedAt"] is not None


async def test_history_grows_with_the_thread(api: Api) -> None:
    thread = await add_thread(api.sm, api.user_id)
    before = {s["kind"]: s["tokens"] for s in (await get_context(api, thread)).json()["segments"]}
    await add_message(api.sm, api.user_id, thread, "user", "слово " * 300)
    after = {s["kind"]: s["tokens"] for s in (await get_context(api, thread)).json()["segments"]}
    assert after["history"] > before["history"] + 500
    assert after["system"] == before["system"]


async def test_a_foreign_thread_and_foreign_job_results_are_not_visible(api: Api) -> None:
    other = await add_user(api.sm, "other")
    theirs = await add_thread(api.sm, other)
    async with api.sm() as session:
        session.add(
            Job(
                user_id=other,
                kind="chat",
                state="done",
                thread_id=theirs,
                result={"context": {"data": 999}, "sources": [{"title": "Чужое"}]},
                run_after=NOW,
            )
        )
        await session.commit()
    assert (await get_context(api, theirs)).status_code == 404

    mine = await add_thread(api.sm, api.user_id)
    body = (await get_context(api, mine)).json()
    assert body["sources"] == []
    assert next(s for s in body["segments"] if s["kind"] == "data")["tokens"] == 0
