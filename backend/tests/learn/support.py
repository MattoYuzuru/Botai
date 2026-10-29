"""A browser, a model and answers for exploration tests, built from the synthetic fakeuni."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from botai.contracts.agent_ui import (
    AgentUiBlock,
    ChoiceBlock,
    ConfirmBlock,
    InputBlock,
)
from botai.core.browser import FakeBrowserGateway, FakePage, ObservedRequest
from botai.core.jobs import InMemoryJobContext, JobState
from botai.core.llm import LlmRequest, LlmResponse, LlmUsage, ScriptedLlm
from tests.fakeuni import api as fakeuni_api
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
    moved = fakeuni_api.STATE.timetable_version == "v3"
    timetable_path = "/api/v3/schedule" if moved else "/api/v2/timetable"
    timetable = await fetch(app, timetable_path, token)
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
            auth_proven=True,
            has_cookie=True,
        )

    def document(path: str) -> list[ObservedRequest]:
        """The page itself, as a real browser logs it, so that an HTML endpoint has evidence."""
        return [
            ObservedRequest(
                method="GET",
                url=f"{ORIGIN}{path}",
                status=200,
                resource_type="document",
                response_content_type="text/html",
                auth_proven=True,
                has_cookie=True,
            )
        ]

    xhr = [
        req("/api/v2/me", me),
        req(f"{timetable_path}?from=2026-10-26&to=2026-11-01", timetable),
        req("/api/v2/tasks/deadlines", deadlines),
    ]
    pages = {
        f"{ORIGIN}/": FakePage(shell, "Личный кабинет", requests=xhr),
        f"{ORIGIN}/app": FakePage(shell, "Личный кабинет", requests=xhr),
        f"{ORIGIN}/news": FakePage(news, "Новости", requests=document("/news")),
        f"{ORIGIN}/portal/deadlines": FakePage(
            table, "Задания", requests=document("/portal/deadlines")
        ),
        # The synthetic menu has a dead documentation link. A real Chromium navigation
        # receives the source's 404 page; it is not a missing FakeBrowser fixture/transport.
        f"{ORIGIN}/api/docs": FakePage("<main>Страница не найдена</main>", "Not Found"),
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


_OBSERVED_REQUEST = re.compile(
    r"^\[request\] (?P<method>[A-Z]+) (?P<url>\S+) -> (?P<status>\d+)\b.*evidenceRef=(?P<ref>\w+)$",
    re.MULTILINE,
)


def cite_evidence(request: LlmRequest, response: LlmResponse) -> LlmResponse:
    """Fill in `requestEvidenceId` the way a real author does: by naming the observed request an
    endpoint (or a bootstrap step) reads. A scripted answer stays free of per-run reference ids."""
    try:
        body = json.loads(response.content)
    except ValueError:
        return response
    if not isinstance(body, dict) or not isinstance(body.get("endpoints"), list):
        return response
    text = "\n".join(message.content for message in request.messages)
    observed = [
        (urlsplit(m["url"]), m["ref"])
        for m in _OBSERVED_REQUEST.finditer(text)
        if m["method"] == "GET" and m["status"].startswith("2")
    ]

    def cite(step: dict[str, Any]) -> None:
        if "requestEvidenceId" in step or "browserPlanRef" in step:
            return
        spec = step.get("request", step)
        keys = {k for k in (spec.get("query") or {})}
        candidates = [(u, ref) for u, ref in observed if u.path == spec.get("path")]
        exact = [(u, ref) for u, ref in candidates if {k for k, _ in parse_qsl(u.query)} == keys]
        # A model that invents an address still has to cite some observed request.
        chosen = (exact or candidates or observed or [(None, None)])[0][1]
        if chosen is not None:
            step["requestEvidenceId"] = chosen

    for endpoint in body["endpoints"]:
        if isinstance(endpoint, dict):
            cite(endpoint)
    for step in body.get("bootstrap") or []:
        if isinstance(step, dict):
            cite(step)
    return response.model_copy(update={"content": json.dumps(body, ensure_ascii=False)})


class EvidenceLlm(ScriptedLlm):
    """A scripted author whose endpoints cite the observed request they read."""

    async def complete(self, request: LlmRequest) -> LlmResponse:
        response = await super().complete(request)
        if request.purpose in {"learn", "repair"}:
            return cite_evidence(request, response)
        return response


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


class StoppedByStudent(InMemoryJobContext):
    """A job the student stops. The runner cancels its task, so the handler is not told by a
    `JobCancelled` from a wait: it gets a `CancelledError` at whatever it awaits next."""

    async def status(self, stage: str, *, state: JobState = JobState.RUNNING) -> None:
        raise asyncio.CancelledError
