"""Job runner -> usage scope -> DeepSeekProvider -> quota and `llm_usage`, on a mocked model API."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from botai.agent.llm import DbUsageGate, DeepSeekProvider
from botai.core.jobs import JobContext, JobKind
from botai.core.llm import LlmMessage, LlmRequest, LlmUsage
from botai.core.services import Services
from botai.db.models import LlmUsageRow
from botai.settings import Settings
from botai.usage import service
from tests.jobs.env import Env

pytestmark = pytest.mark.db

Payload = dict[str, Any]


def _sse(text: str = "ок") -> httpx.Response:
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "prompt_cache_hit_tokens": 800,
                "prompt_cache_miss_tokens": 200,
            },
        },
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


class Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return _sse()


def with_model(env: Env, wire: Wire) -> Services:
    settings = Settings(
        _env_file=None, env="test", llm_provider="deepseek", deepseek_api_key="sk-test-FIXTURE"
    )  # type: ignore[arg-type,call-arg]
    provider = DeepSeekProvider(
        settings,
        gate=DbUsageGate(lambda: env.sm),
        transport=httpx.MockTransport(wire),
        now=env.clock.now,
    )
    return dataclasses.replace(env.services, llm=provider)


def real_now() -> datetime:
    return datetime.now(UTC)


def ask_model(question: str = "привет") -> LlmRequest:
    return LlmRequest(purpose="agent", messages=[LlmMessage(role="user", content=question)])


async def test_a_handlers_model_calls_are_recorded_for_its_user_and_job(env: Env) -> None:
    wire = Wire()
    services = with_model(env, wire)

    async def handler(ctx: JobContext, svc: Services, payload: Payload) -> Payload:
        first = await svc.llm.complete(ask_model())
        await svc.llm.complete(ask_model("ещё"))
        return {"answer": first.content}

    acc = await env.account()
    job_id = await env.services.queue.enqueue(JobKind.CHAT, user_id=acc.id, thread_id="t")
    env.start(env.worker({JobKind.CHAT: handler}, services=services))
    job = await env.wait_for(job_id, {"done"})

    assert job.result == {"answer": "ок"}
    assert len(wire.requests) == 2
    async with env.sm() as s:
        rows = (await s.scalars(select(LlmUsageRow).where(LlmUsageRow.user_id == acc.id))).all()
    assert len(rows) == 2
    for row in rows:
        assert row.job_id == job_id
        assert row.purpose == "agent"
        assert (row.input_cache_miss, row.input_cache_hit, row.output) == (200, 800, 200)
        assert float(row.cost_usd) == pytest.approx(
            service.cost_usd(
                LlmUsage(input_cache_miss=200, input_cache_hit=800, output=200, model=row.model),
                peak=False,
            ),
            abs=1e-6,
        )
    # and the user's quota meter moved
    async with env.sm() as s:
        assert await service.tokens_used_today(s, acc.id, real_now()) == 2 * 1_200


async def test_a_user_over_the_budget_gets_a_quota_error_and_no_model_call(env: Env) -> None:
    wire = Wire()
    services = with_model(env, wire)

    async def handler(ctx: JobContext, svc: Services, payload: Payload) -> None:
        await svc.llm.complete(ask_model())

    acc = await env.account()
    async with env.sm() as s:
        await service.record_usage(
            s,
            user_id=acc.id,
            job_id=None,
            purpose="agent",
            usage=LlmUsage(
                input_cache_miss=services.settings.quota_tokens_per_day, model="deepseek-flash"
            ),
            peak=False,
            now=real_now(),  # DbUsageGate checks against the wall clock
        )
    job_id = await env.services.queue.enqueue(JobKind.CHAT, user_id=acc.id, thread_id="t")
    env.start(env.worker({JobKind.CHAT: handler}, services=services))
    job = await env.wait_for(job_id, {"failed"})

    assert wire.requests == []  # refused before any spend
    assert job.attempts == 1  # a quota error is not retried
    assert job.error is not None
    assert job.error["code"] == "quota_exceeded"
    assert job.error["status"] == 429
    events = await env.events(job_id)
    assert events[-1].type == "error"
    assert events[-1].data["code"] == "quota_exceeded"
    assert events[-1].data["scope"] == "daily"
    expected = service.day_bounds(real_now())[1].astimezone(UTC).isoformat().replace("+00:00", "Z")
    assert events[-1].data["resetsAt"] == expected
    assert "Суточный лимит" in events[-1].data["detail"]


async def test_one_users_loop_does_not_stop_another_user(env: Env) -> None:
    wire = Wire()
    services = with_model(env, wire)

    async def handler(ctx: JobContext, svc: Services, payload: Payload) -> Payload:
        await svc.llm.complete(ask_model())
        return {"user": ctx.user_id}

    runaway = await env.account(email="runaway@example.com")
    calm = await env.account(email="calm@example.com")
    limit = services.settings.quota_requests_per_minute
    async with env.sm() as s:
        for _ in range(limit):
            await service.record_usage(
                s,
                user_id=runaway.id,
                job_id=None,
                purpose="agent",
                usage=LlmUsage(output=1, model="deepseek-flash"),
                peak=False,
                now=real_now(),
            )
    bad = await env.services.queue.enqueue(JobKind.CHAT, user_id=runaway.id, thread_id="t1")
    good = await env.services.queue.enqueue(JobKind.CHAT, user_id=calm.id, thread_id="t2")
    env.start(env.worker({JobKind.CHAT: handler}, services=services))
    assert (await env.wait_for(good, {"done"})).result == {"user": calm.id}
    failed = await env.wait_for(bad, {"failed"})
    assert failed.error is not None
    assert failed.error["extra"]["scope"] == "minute"
