"""The real chat job exposes its trace, activity, and the actual budget-stop reason."""

from datetime import timedelta

import pytest

from botai.agent.threads import mark_read
from botai.agent.v4.providers import MockProvider
from botai.core.llm import LlmRequest, LlmResponse
from botai.db.models import ChatThread
from tests.agent.v4.runtime_support import answer, call, tool_response, usage
from tests.jobs.env import Env
from tests.jobs.test_agent_chat_v4 import (
    FlowProvider,
    _chat_worker,
    _post,
    _student,
    _thread_messages,
)

pytestmark = pytest.mark.db


async def test_chat_token_limit_names_actual_reason_and_exposes_trace(env: Env) -> None:
    client, _, thread, _ = await _student(env)
    env.settings.agent_limits_tokens = 1000
    env.settings.agent_continuations_max = 0  # a heavy segment goes on by itself; this one stops
    provider = MockProvider(
        responses=[
            tool_response(call("fs_list", {}, "c1"), usage=usage(hit=1000)),
        ]
    )
    sent = await _post(client, thread, "Подключи расписание")
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, provider))
    job = await env.wait_for(job_id, {"done"}, within=30)
    assert job.result is not None
    assert job.result["status"] == "token_limit"
    assert job.result["limit_reached"] == "tokens"
    trace = await client.get(f"/api/v1/agent/runs/{job.result['run_id']}")
    assert trace.status_code == 200
    history = await _thread_messages(client, thread)
    text = history[-1]["blocks"][0]["markdown"]
    assert "лимит суммарных токенов" in text
    assert "Работа не завершена" in text
    assert "не хватило шагов" not in text
    progress = history[-2]["blocks"]
    assert any(block["stage"] == "Проверяю файлы" for block in progress)
    assert progress[0]["state"] == "failed"


async def test_chat_tool_activity_arrives_before_final_answer(env: Env) -> None:
    client, _, thread, _ = await _student(env)
    saw_progress = False
    job_id = ""

    # The DB event reader is the same stream's durable backing, and does not wait for job end.
    async def checked(request: LlmRequest) -> LlmResponse:
        nonlocal saw_progress
        if not any(m.role == "tool" for m in request.messages):
            return tool_response(call("fs_list", {}, "c1"))
        events = await env.events(job_id)
        saw_progress = any(
            event.type == "block" and event.data.get("stage") == "Проверяю файлы"
            for event in events
        )
        return answer("Проверено")

    sent = await _post(client, thread, "Проверь файлы")
    job_id = sent.json()["jobId"]
    env.start(await _chat_worker(env, FlowProvider(checked)))
    await env.wait_for(job_id, {"done"}, within=30)
    assert saw_progress


async def test_delayed_read_request_does_not_undo_a_newer_read(env: Env) -> None:
    _, user, thread, _ = await _student(env)
    newer = env.clock.now()
    older = newer - timedelta(microseconds=2)
    async with env.sm() as first, env.sm() as delayed:
        # Both tabs loaded the same old watermark before either request completed.
        stale = await delayed.get(ChatThread, thread)
        assert stale is not None
        assert stale.last_read_at is None
        await mark_read(first, env.services, user, thread, newer)
        await mark_read(delayed, env.services, user, thread, older)
    async with env.sm() as session:
        current = await session.get(ChatThread, thread)
        assert current is not None
        assert current.last_read_at == newer
