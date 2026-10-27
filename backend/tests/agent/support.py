"""Helpers for agent tests: a Services container on doubles and a bound test database."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.llm import DeepSeekProvider
from botai.core.browser import FakeBrowserGateway
from botai.core.clock import FrozenClock
from botai.core.embed import NoopEmbedder
from botai.core.errors import BotaiError, ErrorCode
from botai.core.llm import LlmMessage, LlmProvider, LlmRequest, LlmUsage
from botai.core.queue import InMemoryJobQueue
from botai.core.services import Services
from botai.core.vault import InMemoryVault
from botai.settings import Settings
from tests.agent.conftest import make_settings

NOW = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)  # Wednesday; the fakeuni week starts 2026-10-26


class NoSessions:
    async def ensure(self, ctx: object, *, user_id: str, connection_id: str) -> Any:
        raise AssertionError("the chat agent never logs in to a university")


def make_services(
    llm: LlmProvider,
    *,
    sm: async_sessionmaker[AsyncSession] | None = None,
    queue: InMemoryJobQueue | None = None,
    settings: Settings | None = None,
    now: datetime = NOW,
) -> Services:
    return Services(
        settings=settings or make_settings(llm_provider="mock"),
        db=sm,  # type: ignore[arg-type]
        clock=FrozenClock(now),
        vault=InMemoryVault(),
        llm=llm,
        browser=FakeBrowserGateway({}, now, "https://fakeuni.test"),
        sessions=NoSessions(),
        queue=queue or InMemoryJobQueue(),
        embedder=NoopEmbedder(),
    )


Handler = Callable[[httpx.Request], httpx.Response]


def sse(*chunks: dict[str, Any]) -> httpx.Response:
    text = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    return httpx.Response(
        200,
        content=(text + "data: [DONE]\n\n").encode(),
        headers={"content-type": "text/event-stream"},
    )


def answer_chunks(
    text: str = "ок", *, reasoning: str | None = None, usage: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    if reasoning:
        chunks.append({"choices": [{"index": 0, "delta": {"reasoning_content": reasoning}}]})
    chunks.append({"choices": [{"index": 0, "delta": {"content": text}}]})
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    chunks.append(
        {
            "choices": [],
            "usage": usage
            or {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": 12},
            },
        }
    )
    return chunks


class FakeGate:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.checked: list[str] = []
        self.recorded: list[dict[str, Any]] = []

    async def check(self, user_id: str) -> None:
        self.checked.append(user_id)
        if self.deny:
            raise BotaiError(ErrorCode.QUOTA_EXCEEDED, "Дневной лимит исчерпан")

    async def record(
        self, *, user_id: str, job_id: str | None, purpose: str, usage: LlmUsage, peak: bool
    ) -> None:
        self.recorded.append(
            {"user_id": user_id, "job_id": job_id, "purpose": purpose, "usage": usage, "peak": peak}
        )


class Wire:
    """Records requests, answers with a script of handlers (the last one repeats)."""

    def __init__(self, *handlers: Handler) -> None:
        self.handlers = list(handlers) or [lambda _r: sse(*answer_chunks())]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.handlers) - 1)
        return self.handlers[index](request)

    def body(self, n: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[n].content)


def provider(
    wire: Wire,
    *,
    gate: FakeGate | None = None,
    stream: bool = True,
    sleeps: list[float] | None = None,
    **settings: object,
) -> DeepSeekProvider:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return DeepSeekProvider(
        make_settings(**settings),
        gate=gate or FakeGate(),
        stream=stream,
        transport=httpx.MockTransport(wire),
        sleep=sleep,
        now=lambda: datetime(2026, 10, 31, 12, 0, tzinfo=UTC),  # Saturday: off-peak
    )


def req(text: str = "привет", **kw: Any) -> LlmRequest:
    return LlmRequest(messages=[LlmMessage(role="user", content=text)], **kw)


async def add_thread(
    sm: async_sessionmaker[AsyncSession], user_id: str, *, space_id: str | None = None
) -> str:
    from botai.core.ids import new_id
    from botai.db.models import ChatThread

    thread_id = new_id()
    async with sm() as session:
        session.add(ChatThread(id=thread_id, user_id=user_id, space_id=space_id))
        await session.commit()
    return thread_id


async def add_message(
    sm: async_sessionmaker[AsyncSession],
    user_id: str,
    thread_id: str,
    role: str,
    text: str,
    *,
    at: datetime | None = None,
) -> str:
    from botai.core.ids import new_id
    from botai.db.models import ChatMessage

    message_id = new_id()
    async with sm() as session:
        session.add(
            ChatMessage(
                id=message_id,
                user_id=user_id,
                thread_id=thread_id,
                role=role,
                blocks=[{"type": "text", "markdown": text}],
                **({"created_at": at} if at else {}),
            )
        )
        await session.commit()
    return message_id
