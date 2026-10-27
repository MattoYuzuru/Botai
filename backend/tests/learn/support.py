"""A browser, a model and answers for exploration tests, built from the synthetic fakeuni."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from botai.contracts.agent_ui import (
    AgentUiBlock,
    ChoiceBlock,
    ConfirmBlock,
    InputBlock,
)
from botai.core.browser import FakeBrowserGateway, FakePage, ObservedRequest
from botai.core.jobs import InMemoryJobContext
from botai.core.llm import LlmResponse, LlmUsage
from tests.fakeuni import data
from tests.fakeuni.login import COOKIE
from tests.sources import recipes
from tests.sources.helpers import NOW, new_session_token

ORIGIN = data.ORIGIN


def fixed(**kw: Any) -> dict[str, Any]:
    return kw


async def fetch(app: object, path: str, token: str) -> str:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url=ORIGIN,
        cookies={COOKIE: token},
    ) as client:
        return (await client.get(path)).text


async def fake_site(app: object, *, home_extra: str = "") -> FakeBrowserGateway:
    """What a logged-in browser would see on fakeuni: pages and the XHR behind them."""
    token = new_session_token("tok-observer")
    shell = await fetch(app, "/app", token)
    if home_extra:
        shell = shell.replace("<div id='root'>", f"<p>{home_extra}</p><div id='root'>")
    me = await fetch(app, "/api/v2/me", token)
    timetable = await fetch(app, "/api/v2/timetable", token)
    deadlines = await fetch(app, "/api/v2/tasks/deadlines", token)
    news = await fetch(app, "/news", token)
    table = await fetch(app, "/portal/deadlines", token)

    def req(path: str, body: str) -> ObservedRequest:
        return ObservedRequest(
            method="GET",
            url=f"{ORIGIN}{path}",
            status=200,
            resource_type="xhr",
            response_content_type="application/json",
            response_sample=body,
        )

    xhr = [
        req("/api/v2/me", me),
        req("/api/v2/timetable?from=2026-10-26&to=2026-11-01", timetable),
        req("/api/v2/tasks/deadlines", deadlines),
    ]
    pages = {
        f"{ORIGIN}/": FakePage(shell, "Личный кабинет", requests=xhr),
        f"{ORIGIN}/app": FakePage(shell, "Личный кабинет", requests=xhr),
        f"{ORIGIN}/news": FakePage(news, "Новости"),
        f"{ORIGIN}/portal/deadlines": FakePage(table, "Задания"),
    }
    return FakeBrowserGateway(pages, NOW, ORIGIN)


def answer(
    endpoints: list[dict[str, Any]] | None,
    *,
    bootstrap: list[dict[str, Any]] | None = None,
    reasoning: str = "Нашла подходящий запрос.",
    doubts: list[str] | None = None,
) -> LlmResponse:
    """A model answer in the shape of the authoring schema."""
    body = {
        "found": bool(endpoints),
        "reasoning": reasoning,
        "endpoints": endpoints or [],
        "bootstrap": bootstrap or [],
        "auth": {"type": "cookie"},
        "doubts": doubts or [],
    }
    return LlmResponse(
        content=json.dumps(body, ensure_ascii=False),
        usage=LlmUsage(input_cache_miss=1200, output=300, model="scripted"),
    )


def timetable_answer() -> LlmResponse:
    return answer([recipes.timetable_endpoint()])


def deadlines_answer() -> LlmResponse:
    return answer([recipes.deadlines_endpoint()])


def nothing() -> LlmResponse:
    return answer(None, reasoning="Подходящих данных не видно.")


Responder = Callable[[AgentUiBlock], str | list[str] | bool | None]


def scripted_user(
    user_id: str,
    connection_id: str,
    *,
    confirm: bool = True,
    links: list[str] | None = None,
    choices: list[str] | None = None,
) -> tuple[InMemoryJobContext, list[AgentUiBlock]]:
    """A job context whose "user" confirms, picks from `choices` in order, gives `links`."""
    asked: list[AgentUiBlock] = []
    link_queue = list(links or [])
    choice_queue = list(choices or [])

    def respond(block: AgentUiBlock) -> str | list[str] | bool | None:
        asked.append(block)
        if isinstance(block, ConfirmBlock):
            return confirm
        if isinstance(block, ChoiceBlock):
            return choice_queue.pop(0) if choice_queue else block.options[0].value
        if isinstance(block, InputBlock):
            return link_queue.pop(0) if link_queue else None
        return None

    ctx = InMemoryJobContext(user_id=user_id, connection_id=connection_id, responder=respond)
    return ctx, asked


def blocks_of(ctx: InMemoryJobContext, kind: str) -> list[Any]:
    return [b for b in ctx.blocks if b.type == kind]


def all_text(ctx: InMemoryJobContext) -> str:
    return json.dumps(
        [b.model_dump(mode="json", by_alias=True) for b in ctx.blocks], ensure_ascii=False
    )
