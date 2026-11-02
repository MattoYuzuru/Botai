"""Optional next-turn drafts: HTTP ownership, staleness, privacy and bounded paid work (L1)."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from botai.agent import suggestions
from botai.agent.llm import current_scope, usage_scope
from botai.core.ids import new_id
from botai.core.llm import LlmRequest, LlmResponse, ScriptedLlm
from botai.db.models import ChatMessage, Job
from tests.agent.conftest import Api, add_user
from tests.agent.support import NOW, add_message, add_thread


async def answer(api: Api) -> tuple[str, str]:
    thread = await add_thread(api.sm, api.user_id)
    await add_message(api.sm, api.user_id, thread, "user", "Что по дедлайнам?", at=NOW)
    anchor = await add_message(
        api.sm, api.user_id, thread, "agent", "Завтра сдача эссе.", at=NOW + timedelta(seconds=1)
    )
    return thread, anchor


async def request(api: Api, thread: str, anchor: str) -> dict[str, object]:
    async with api.client() as client:
        response = await client.post(
            f"/api/v1/chat/threads/{thread}/suggestion", json={"afterMessageId": anchor}
        )
    assert response.status_code == 200
    return response.json()


async def test_suggestion_is_ephemeral_cached_and_uses_cheap_profile(api: Api) -> None:
    thread, anchor = await answer(api)
    llm = ScriptedLlm(queue=[LlmResponse(content="Помоги составить план эссе")])
    api.services.llm = llm
    expected = {"text": "Помоги составить план эссе", "afterMessageId": anchor}
    assert await request(api, thread, anchor) == expected
    assert await request(api, thread, anchor) == expected
    assert len(llm.requests) == 1
    call = llm.requests[0]
    assert (call.purpose, call.tier, call.thinking, call.max_tokens) == (
        "classify",
        "fast",
        "disabled",
        120,
    )
    assert call.tools == []
    async with api.sm() as session:
        assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 2
        assert await session.scalar(select(func.count()).select_from(Job)) == 0
    assert api.queue.jobs == []


async def test_suggestion_requires_auth_and_owned_thread(api: Api) -> None:
    other = await add_user(api.sm, "other")
    thread = await add_thread(api.sm, other)
    async with api.client() as client:
        path = f"/api/v1/chat/threads/{thread}/suggestion"
        assert (await client.post(path, json={"afterMessageId": new_id()})).status_code == 404
        api.app.dependency_overrides.clear()
        assert (await client.post(path, json={"afterMessageId": new_id()})).status_code == 401


@pytest.mark.parametrize(
    "result", [None, {}, {"status": "done", "stopped": "answer", "limit_reached": None}]
)
async def test_completed_job_can_offer_a_suggestion(
    api: Api, result: dict[str, Any] | None
) -> None:
    thread, anchor = await answer(api)
    async with api.sm() as session:
        job = Job(
            user_id=api.user_id,
            thread_id=thread,
            kind="chat",
            state="done",
            result=result,
            run_after=NOW,
        )
        session.add(job)
        await session.flush()
        row = await session.get(ChatMessage, anchor)
        assert row is not None
        row.job_id = job.id
        await session.commit()
    api.services.llm = ScriptedLlm(queue=[LlmResponse(content="Помоги с эссе")])
    assert (await request(api, thread, anchor))["text"] == "Помоги с эссе"


@pytest.mark.parametrize(
    "result",
    [
        *(
            {"status": status}
            for status in (
                "token_limit",
                "time_limit",
                "step_limit",
                "cost_limit",
                "hard_stop",
                "paused",
                "cancelled",
                "error",
            )
        ),
        {"stopped": "budget"},
        {"stopped": "error"},
        {"stopped": "quota"},
        {"status": "done", "limit_reached": "tokens"},
    ],
)
async def test_done_job_with_incomplete_agent_result_does_not_offer_a_suggestion(
    api: Api, result: dict[str, Any]
) -> None:
    thread, anchor = await answer(api)
    async with api.sm() as session:
        job = Job(
            user_id=api.user_id,
            thread_id=thread,
            kind="chat",
            state="done",
            result=result,
            run_after=NOW,
        )
        session.add(job)
        await session.flush()
        row = await session.get(ChatMessage, anchor)
        assert row is not None
        row.job_id = job.id
        await session.commit()
    llm = ScriptedLlm()
    api.services.llm = llm
    assert await request(api, thread, anchor) == {"text": None, "afterMessageId": anchor}
    assert llm.requests == []


async def test_cache_expires_and_retains_only_bounded_answers(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(suggestions, "_cache", OrderedDict())
    monkeypatch.setattr(suggestions, "CACHE_LIMIT", 2)
    llm = ScriptedLlm(queue=[LlmResponse(content="Помоги с эссе")] * 4)
    api.services.llm = llm
    anchors = [await answer(api) for _ in range(3)]
    for thread, anchor in anchors:
        assert (await request(api, thread, anchor))["text"] == "Помоги с эссе"
    assert len(suggestions._cache) == 2
    assert (api.user_id, *anchors[0]) not in suggestions._cache
    # The last answer is present but expired: a fresh call can recover a previous empty draft.
    key = (api.user_id, *anchors[-1])
    suggestions._cache[key] = (0.0, None)
    assert (await request(api, *anchors[-1]))["text"] == "Помоги с эссе"
    assert len(llm.requests) == 4


@pytest.mark.parametrize("state", ["queued", "running", "waiting_user", "failed", "cancelled"])
async def test_suggestion_waits_for_completed_anchor_job(api: Api, state: str) -> None:
    thread, anchor = await answer(api)
    async with api.sm() as session:
        job = Job(user_id=api.user_id, thread_id=thread, kind="chat", state=state, run_after=NOW)
        session.add(job)
        await session.flush()
        row = await session.get(ChatMessage, anchor)
        assert row is not None
        row.job_id = job.id
        await session.commit()
    llm = ScriptedLlm()
    api.services.llm = llm
    assert await request(api, thread, anchor) == {"text": None, "afterMessageId": anchor}
    assert llm.requests == []


async def test_any_active_task_suppresses_cached_suggestion(api: Api) -> None:
    thread, anchor = await answer(api)
    api.services.llm = ScriptedLlm(queue=[LlmResponse(content="Помоги с эссе")])
    assert (await request(api, thread, anchor))["text"] == "Помоги с эссе"
    async with api.sm() as session:
        session.add(
            Job(
                user_id=api.user_id,
                thread_id=thread,
                kind="connect",
                state="waiting_user",
                payload={"dialogue": {"pending": {"secret": True}}},
                run_after=NOW,
            )
        )
        await session.commit()
    assert (await request(api, thread, anchor))["text"] is None


async def test_only_exact_latest_agent_text_is_eligible(api: Api) -> None:
    thread, anchor = await answer(api)
    llm = ScriptedLlm()
    api.services.llm = llm
    assert (await request(api, thread, new_id()))["text"] is None
    user_message = await add_message(
        api.sm, api.user_id, thread, "user", "Уже сделал", at=NOW + timedelta(seconds=2)
    )
    assert (await request(api, thread, anchor))["text"] is None
    assert (await request(api, thread, user_message))["text"] is None
    assert llm.requests == []


async def test_context_contains_only_masked_public_recent_text(api: Api) -> None:
    thread, anchor = await answer(api)
    async with api.sm() as session:
        row = await session.get(ChatMessage, anchor)
        assert row is not None
        row.blocks += [
            {
                "type": "text",
                "markdown": "Пароль: DummyPass42!\nКод из смс 839102\n"
                "https://calendar.fakeuni.test/private/SECRET_CALENDAR.ics",
            },
            {"type": "input", "secret": True, "value": "INPUT_SECRET"},
            {"type": "attachment", "name": "FILE_SECRET"},
        ]
        await session.commit()
    llm = ScriptedLlm(queue=[LlmResponse(content="Когда дедлайн?")])
    api.services.llm = llm
    assert (await request(api, thread, anchor))["text"] == "Когда дедлайн?"
    context = llm.requests[0].messages[1].content
    for secret in ("DummyPass42!", "839102", "SECRET_CALENDAR", "INPUT_SECRET", "FILE_SECRET"):
        assert secret not in context
    assert "Завтра сдача эссе" in context
    assert json.loads(context)[0]["role"] == "user"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x" * 241,
        "Пароль: DummyPass42!",
        "Код из смс 839102",
        "https://example.test/",
        "- Первый вариант\n- Второй",
    ],
)
async def test_unusable_or_secret_output_is_silently_absent(api: Api, value: str) -> None:
    thread, anchor = await answer(api)
    api.services.llm = ScriptedLlm(queue=[LlmResponse(content=value)])
    assert (await request(api, thread, anchor))["text"] is None


async def test_failure_is_silent_and_cached(api: Api) -> None:
    thread, anchor = await answer(api)
    llm = ScriptedLlm()  # Unknown request raises; no provider error is sent to the browser.
    api.services.llm = llm
    assert (await request(api, thread, anchor))["text"] is None
    assert (await request(api, thread, anchor))["text"] is None
    assert len(llm.requests) == 1


async def test_timeout_is_silent(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    thread, anchor = await answer(api)

    class Slow:
        async def complete(self, request: LlmRequest) -> LlmResponse:
            await asyncio.Event().wait()
            return LlmResponse(content="Не дождались")

    api.services.llm = Slow()
    monkeypatch.setattr(suggestions, "TIMEOUT_S", 0.01)
    assert (await request(api, thread, anchor))["text"] is None
    assert not suggestions._in_flight


async def test_accounting_scope_is_user_only_and_restores_caller(api: Api) -> None:
    thread, anchor = await answer(api)

    class Scoped:
        async def complete(self, request: LlmRequest) -> LlmResponse:
            scope = current_scope()
            assert scope is not None
            assert (scope.user_id, scope.job_id) == (api.user_id, None)
            return LlmResponse(content="Помоги с эссе")

    api.services.llm = Scoped()
    with usage_scope("outer-user", "outer-job") as outer:
        assert (await request(api, thread, anchor))["text"] == "Помоги с эссе"
        assert current_scope() == outer


@pytest.mark.parametrize("change", ["message", "job", "result"])
async def test_anchor_is_revalidated_after_model(api: Api, change: str) -> None:
    thread, anchor = await answer(api)
    async with api.sm() as session:
        job = Job(user_id=api.user_id, thread_id=thread, kind="chat", state="done", run_after=NOW)
        session.add(job)
        await session.flush()
        anchor_job_id = job.id
        row = await session.get(ChatMessage, anchor)
        assert row is not None
        row.job_id = anchor_job_id
        await session.commit()

    class Moving:
        async def complete(self, request: LlmRequest) -> LlmResponse:
            if change == "message":
                await add_message(
                    api.sm, api.user_id, thread, "user", "Спасибо", at=NOW + timedelta(seconds=2)
                )
            elif change == "job":
                async with api.sm() as session:
                    session.add(
                        Job(
                            user_id=api.user_id,
                            thread_id=thread,
                            kind="chat",
                            state="running",
                            run_after=NOW,
                        )
                    )
                    await session.commit()
            else:
                async with api.sm() as session:
                    job = await session.get(Job, anchor_job_id)
                    assert job is not None
                    job.result = {"status": "paused"}
                    await session.commit()
            return LlmResponse(content="Помоги с эссе")

    api.services.llm = Moving()
    assert (await request(api, thread, anchor))["text"] is None


async def test_concurrent_requests_share_one_model_call(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread, anchor = await answer(api)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    class Blocking:
        async def complete(self, request: LlmRequest) -> LlmResponse:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return LlmResponse(content="Помоги с эссе")

    api.services.llm = Blocking()
    # The normal fixture shares one transaction/connection; independent overlapping savepoints
    # cannot represent concurrent HTTP clients. Exercise the real task/cache coordination while
    # replacing only the already-covered database eligibility/context reads.
    monkeypatch.setattr(suggestions, "owned_thread", AsyncMock())
    monkeypatch.setattr(suggestions, "_eligible", AsyncMock(return_value=True))
    monkeypatch.setattr(suggestions, "_context", AsyncMock(return_value="[]"))
    session = AsyncMock()
    session.scalar.return_value = thread
    first = asyncio.create_task(
        suggestions.suggest(session, api.services, api.user_id, thread, anchor)
    )
    await started.wait()
    second = asyncio.create_task(
        suggestions.suggest(session, api.services, api.user_id, thread, anchor)
    )
    await asyncio.sleep(0.02)
    release.set()
    results = await asyncio.gather(first, second)
    assert calls == 1
    assert [result.text for result in results] == ["Помоги с эссе"] * 2
