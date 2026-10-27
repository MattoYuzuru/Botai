"""Rig for end-to-end chat tests: a student with a space, a connection and a thread."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.chat import handle_chat
from botai.contracts.agent_ui import TextBlock
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext
from botai.core.llm import LlmProvider, LlmResponse, LlmToolCall, LlmUsage
from botai.core.queue import InMemoryJobQueue
from botai.core.services import Services
from botai.db.models import ChatMessage
from tests.agent.seed import FakeReader, add_connection, add_space
from tests.agent.support import NOW, add_message, add_thread, make_services

Sm = async_sessionmaker[AsyncSession]


def call(name: str, call_id: str = "c1", **args: Any) -> LlmToolCall:
    return LlmToolCall(id=call_id, name=name, arguments=args)


def step(*calls: LlmToolCall) -> LlmResponse:
    return LlmResponse(
        tool_calls=list(calls),
        reasoning_content="думаю",
        usage=LlmUsage(input_cache_miss=100, output=10, model="deepseek-flash"),
    )


def final(text: str) -> LlmResponse:
    return LlmResponse(content=text, usage=LlmUsage(input_cache_miss=100, output=10))


@dataclass
class Rig:
    sm: Sm
    user_id: str
    space_id: str
    conn_id: str
    thread_id: str
    llm: Any
    queue: InMemoryJobQueue
    services: Services

    async def ask(
        self, text: str, *, at: datetime | None = None, reader: FakeReader | None = None
    ) -> tuple[InMemoryJobContext, dict[str, Any] | None]:
        qid = await add_message(
            self.sm, self.user_id, self.thread_id, "user", text, at=at or NOW - timedelta(minutes=1)
        )
        ctx = InMemoryJobContext(user_id=self.user_id, job_id=new_id())
        out = await handle_chat(
            ctx, self.services, {"thread_id": self.thread_id, "message_id": qid}, reader=reader
        )
        return ctx, out

    def sent(self) -> str:
        """Everything that went to the model, as one string."""
        return json.dumps(
            [r.model_dump(mode="json") for r in self.llm.requests], ensure_ascii=False
        )


async def make_rig(
    sm: Sm,
    user_id: str,
    llm: LlmProvider,
    *,
    conn_status: str = "active",
    space_profile: dict[str, Any] | None = None,
    settings: Any = None,
) -> Rig:
    space = await add_space(sm, user_id, "Вуз А", profile=space_profile)
    conn = await add_connection(sm, user_id, space, "Расписание ЛК", status=conn_status)
    thread = await add_thread(sm, user_id, space_id=space)
    queue = InMemoryJobQueue()
    services = make_services(llm, sm=sm, queue=queue, settings=settings)
    return Rig(sm, user_id, space, conn, thread, llm, queue, services)


def llm_requests(r: Rig) -> list[Any]:
    return r.llm.requests


def text_block(ctx: InMemoryJobContext) -> TextBlock:
    blocks = [b for b in ctx.blocks if isinstance(b, TextBlock)]
    assert len(blocks) == 1
    return blocks[0]


async def agent_messages(sm: Sm, thread_id: str) -> list[ChatMessage]:
    async with sm() as session:
        rows = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread_id, ChatMessage.role == "agent")
            .order_by(ChatMessage.created_at)
        )
        return list(rows.scalars())
