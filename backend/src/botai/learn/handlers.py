"""Job handlers owned by this module (see docs/plan/workstreams.md). Keep the HANDLERS name."""

from __future__ import annotations

from typing import Any

import httpx

from botai.core.errors import BotaiError, ErrorCode
from botai.core.handlers import HandlerTable
from botai.core.http import RetryPolicy
from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services
from botai.learn.explorer import Explorer


def _connection_id(ctx: JobContext, payload: dict[str, Any]) -> str:
    connection_id = ctx.connection_id or str(payload.get("connection_id", ""))
    if not connection_id:
        raise BotaiError(ErrorCode.VALIDATION, "У задания обучения нет подключения")
    return connection_id


def make_handlers(
    *, transport: httpx.AsyncBaseTransport | None = None, retry: RetryPolicy | None = None
) -> HandlerTable:
    """`transport` and `retry` exist for tests; production uses the egress proxy and defaults."""

    async def connect(
        ctx: JobContext, services: Services, payload: dict[str, Any]
    ) -> dict[str, Any]:
        explorer = Explorer(ctx, services, transport=transport, retry=retry)
        return await explorer.connect(_connection_id(ctx, payload), payload)

    async def learn(ctx: JobContext, services: Services, payload: dict[str, Any]) -> dict[str, Any]:
        explorer = Explorer(ctx, services, transport=transport, retry=retry)
        return await explorer.learn(_connection_id(ctx, payload), payload)

    return {JobKind.CONNECT: connect, JobKind.LEARN: learn}


HANDLERS: HandlerTable = make_handlers()
