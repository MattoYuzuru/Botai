"""Agent discovery through the real CHAT job: more data than asked, offered, then connected (D-039).

The scripted model reads a schedule source on fakeuni, notices in the cabinet's menu that grades
and a rating exist, verifies them with a real read, asks the student, and only after "yes"
extends the playbook, syncs and offers one extra panel. The tools are the real ones (real DB, the
real source runner over fakeuni), the questions are real choice blocks answered through the API.
"""

from __future__ import annotations

import asyncio
import copy
import json
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from botai.agent.v4.chat import CHAT_SYSTEM_PROMPT, make_chat_handlers
from botai.core.llm import LlmRequest, LlmResponse
from botai.db.models import (
    AgentRun,
    AgentRunEvent,
    Grade,
    Panel,
    RankingEntry,
    Source,
)
from botai.sources.v4 import playbooks
from botai.sources.v4 import service as sources_service
from tests.agent.v4.runtime_support import answer, call, tool_response
from tests.fakeuni import login as fakeuni_login
from tests.jobs.env import Env
from tests.jobs.test_agent_dispatch_v4 import SCHEDULE_SPEC
from tests.panels.test_sync_and_cleanup import SPEC as CABINET_SPEC

pytestmark = pytest.mark.db

BASE = "https://fakeuni.test"
SECRET = "S3cr3t-FIXTURE-Value-9137"

EXTENDED_SPEC: dict[str, Any] = copy.deepcopy(SCHEDULE_SPEC)
EXTENDED_SPEC["reads"] += [r for r in CABINET_SPEC["reads"] if r["kind"] in ("grades", "ranking")]

YES, LATER = 0, 1


class FlowProvider:
    """A scripted provider that answers from what the run already did."""

    name = "flow"

    def __init__(self, respond: Callable[[LlmRequest], Awaitable[LlmResponse]]) -> None:
        self._respond = respond
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return await self._respond(request)

    async def aclose(self) -> None:
        return None


def _steps(request: LlmRequest) -> int:
    return sum(1 for m in request.messages if m.role == "assistant" and m.tool_calls)


def _dumped(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _tool_text(provider: FlowProvider) -> str:
    return "\n".join(
        message.content
        for request in provider.requests
        for message in request.messages
        if message.role == "tool"
    )


@pytest.fixture
def fakeuni_cookies() -> dict[str, str]:
    token = secrets.token_hex(16)
    fakeuni_login.SESSIONS.add(token)
    return {fakeuni_login.COOKIE: token}


@pytest.fixture
def fakeuni_transport(fakeuni_app: Any) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=fakeuni_app)


async def _student(env: Env) -> tuple[httpx.AsyncClient, str, str, str, str]:
    """(client, user_id, thread_id, space_id, source_id): a student with a schedule source."""
    client = env.new_client()
    account = await env.account(client)
    space = await client.post("/api/v1/spaces", json={"title": "Тестовый вуз"})
    assert space.status_code in (200, 201), space.text
    space_id = space.json()["id"]
    thread = await client.post("/api/v1/chat/threads", json={"spaceId": space_id})
    assert thread.status_code == 201, thread.text
    async with env.sm() as session:
        source = await sources_service.create_source(
            session,
            user_id=account.id,
            space_id=space_id,
            title="Личный кабинет",
            entry_url=f"{BASE}/app",
            kinds=["lms", "schedule"],
        )
        await session.commit()
        source_id = source.id
    return client, account.id, thread.json()["id"], space_id, source_id


async def _start(
    env: Env,
    client: httpx.AsyncClient,
    thread: str,
    provider: FlowProvider,
    text: str,
    *,
    transport: httpx.ASGITransport | None = None,
    cookies: dict[str, str] | None = None,
    tmp_path: Path,
) -> str:
    env.settings.agent_engine = "v4"
    sent = await client.post(f"/api/v1/chat/threads/{thread}/messages", json={"text": text})
    assert sent.status_code == 202, sent.text
    job_id = str(sent.json()["jobId"])
    env.start(
        env.worker(
            make_chat_handlers(
                provider=provider,
                http_transport=transport,
                source_cookies=cookies,
                artifact_root=str(tmp_path / "artifacts"),
                with_browser=False,
            )
        )
    )
    return job_id


async def _answer_next(
    client: httpx.AsyncClient, thread: str, job_id: str, index: int, seen: set[str]
) -> str:
    """Answer the next unanswered choice card of the thread; returns the card's block id."""
    for _ in range(1500):
        response = await client.get(f"/api/v1/chat/threads/{thread}/runtime")
        assert response.status_code == 200, response.text
        fresh = [p for p in response.json()["pending"] if p["blockId"] not in seen]
        if fresh:
            block_id = str(fresh[0]["blockId"])
            posted = await client.post(
                f"/api/v1/jobs/{job_id}/answers", json={"blockId": block_id, "values": str(index)}
            )
            assert posted.status_code == 204, posted.text
            seen.add(block_id)
            return block_id
        await asyncio.sleep(0.02)
    raise AssertionError("the agent never asked the next question")


async def _choice_questions(env: Env, job_id: str) -> list[dict[str, Any]]:
    return [
        event.data
        for event in await env.events(job_id)
        if event.type == "block" and event.data.get("type") == "choice"
    ]


async def _active_spec(env: Env, user_id: str, source_id: str) -> dict[str, Any]:
    async with env.sm() as session:
        source = await sources_service.get_source(session, user_id=user_id, source_id=source_id)
        assert source.playbook_id is not None
        version = await playbooks.active_playbook_version(
            session, user_id=user_id, playbook_id=source.playbook_id
        )
        assert version is not None
        assert version.spec is not None
        return version.spec


async def _count(env: Env, model: type, source_id: str) -> int:
    async with env.sm() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(model).where(model.source_id == source_id)  # type: ignore[attr-defined]
            )
            or 0
        )


def _script(
    source_id: str, space_id: str, seen_answers: list[str]
) -> Callable[[LlmRequest], Awaitable[LlmResponse]]:
    """The model's plan: read the menu, check the rating, ask, extend, sync, offer a panel."""

    async def respond(request: LlmRequest) -> LlmResponse:
        step = _steps(request)
        if step == 0:
            return tool_response(
                call(
                    "source_read_live", {"source_id": source_id, "url": f"{BASE}/api/v2/menu"}, "c0"
                )
            )
        if step == 1:  # verified with a real read, not guessed from a menu label
            return tool_response(
                call(
                    "source_read_live",
                    {"source_id": source_id, "url": f"{BASE}/api/v2/ratings"},
                    "c1",
                )
            )
        if step == 2:
            return tool_response(
                call(
                    "source_update_playbook",
                    {"source_id": source_id, "spec": SCHEDULE_SPEC, "activate": True},
                    "c2",
                )
            )
        if step == 3:
            return tool_response(
                call(
                    "ask_user",
                    {
                        "question": "В кабинете есть оценки и рейтинг (6 строк). Подключить?",
                        "input_kind": "choice",
                        "options": ["Да, подключить", "Не сейчас"],
                    },
                    "c3",
                )
            )
        tool_text = _tool_text_of(request)
        if step == 4:
            if '"chosen": "Да, подключить"' not in tool_text:
                return answer("Хорошо, оставляю только расписание.")
            return tool_response(
                call(
                    "source_update_playbook",
                    {"source_id": source_id, "spec": EXTENDED_SPEC, "activate": True},
                    "c4",
                )
            )
        if step == 5:
            return tool_response(call("source_sync", {"source_id": source_id}, "c5"))
        if step == 6:
            return tool_response(call("panel_list", {"space_id": space_id}, "c6"))
        if step == 7:
            seen_answers.append(tool_text)
            return tool_response(
                call(
                    "ui_propose_panel",
                    {
                        "space_id": space_id,
                        "source_id": source_id,
                        "key": "grades-table",
                        "type": "table",
                        "title": "Все оценки",
                        "dataset": {"kind": "grades"},
                        "view": {"columns": ["title", "value", "date"], "limit": 20},
                        "rationale": "Оценки таблицей: удобно сверять с зачёткой.",
                    },
                    "c7",
                )
            )
        return answer("Подключил оценки и рейтинг, добавил панель «Все оценки».")

    return respond


def _tool_text_of(request: LlmRequest) -> str:
    return "\n".join(m.content for m in request.messages if m.role == "tool")


# --- the student agrees --------------------------------------------------------------------


async def test_the_agent_finds_grades_and_a_rating_and_connects_them_after_a_yes(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
) -> None:
    client, user, thread, space_id, source_id = await _student(env)
    list_results: list[str] = []
    provider = FlowProvider(_script(source_id, space_id, list_results))
    job_id = await _start(
        env,
        client,
        thread,
        provider,
        "Подключи расписание из кабинета",
        transport=fakeuni_transport,
        cookies=fakeuni_cookies,
        tmp_path=tmp_path,
    )

    seen: set[str] = set()
    await _answer_next(client, thread, job_id, YES, seen)  # «Да, подключить»
    await _answer_next(client, thread, job_id, YES, seen)  # «Добавить панель»
    job = await env.wait_for(job_id, {"done"}, within=60)
    assert job.result is not None

    # The playbook was extended with the two new kinds only after the yes; schedule is intact.
    spec = await _active_spec(env, user, source_id)
    kinds = {read["kind"] for read in spec["reads"]}
    assert {"schedule", "deadlines", "grades", "ranking"} <= kinds
    assert "profile" not in kinds  # the student was offered grades and a rating, nothing more
    assert await _count(env, Grade, source_id) > 0
    assert await _count(env, RankingEntry, source_id) > 0

    # The sync made the system panels by itself; the agent added one custom panel on top.
    async with env.sm() as session:
        panels = {
            p.key: p for p in await session.scalars(select(Panel).where(Panel.space_id == space_id))
        }
    system = {key for key, panel in panels.items() if panel.origin == "system"}
    assert "grades-overview" in system
    assert any(key.startswith("ranking:") for key in system)
    custom = panels["grades-table"]
    assert (custom.origin, custom.status, custom.visible) == ("agent", "active", True)
    assert custom.proposed_by_job_id == job_id
    assert custom.source_id == source_id
    assert custom.spec["icon"] == "table"

    # Two real choice cards: connect the data, then add the panel (with a 500-char-safe preview).
    cards = await _choice_questions(env, job_id)
    assert len(cards) == 2
    assert "Подключить" in cards[0]["question"]
    assert cards[1]["question"].startswith("Добавить в «Учёбу» панель «Все оценки»?")
    assert len(cards[1]["question"]) <= 500
    assert [o["label"] for o in cards[1]["options"]] == ["Добавить панель", "Не сейчас"]

    # panel_list before the proposal showed the system panels, so the agent did not duplicate them.
    assert "grades-overview" in list_results[0]
    # The final answer is stored in the thread and no step failed.
    assert job.result["stopped"] == "answer"


# --- the student declines ------------------------------------------------------------------


async def test_declining_the_offer_changes_nothing(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
) -> None:
    client, user, thread, space_id, source_id = await _student(env)
    provider = FlowProvider(_script(source_id, space_id, []))
    job_id = await _start(
        env,
        client,
        thread,
        provider,
        "Подключи расписание из кабинета",
        transport=fakeuni_transport,
        cookies=fakeuni_cookies,
        tmp_path=tmp_path,
    )

    await _answer_next(client, thread, job_id, LATER, set())  # «Не сейчас»
    job = await env.wait_for(job_id, {"done"}, within=60)
    assert job.result is not None

    # The playbook is still the schedule one; no grade or rating row exists; no panel was made.
    spec = await _active_spec(env, user, source_id)
    assert {read["kind"] for read in spec["reads"]} == {"schedule", "deadlines"}
    assert await _count(env, Grade, source_id) == 0
    assert await _count(env, RankingEntry, source_id) == 0
    async with env.sm() as session:
        assert not list(await session.scalars(select(Panel).where(Panel.space_id == space_id)))
        source = await session.get(Source, source_id)
        assert source is not None
        assert source.playbook_id is not None
    assert len(await _choice_questions(env, job_id)) == 1
    used = {name for request in provider.requests for name in _called(request)}
    assert "ui_propose_panel" not in used
    assert "source_sync" not in used


def _called(request: LlmRequest) -> list[str]:
    return [
        tool_call.name
        for message in request.messages
        if message.role == "assistant" and message.tool_calls
        for tool_call in message.tool_calls
    ]


async def test_refusing_the_panel_dismisses_it_and_leaves_the_data(
    env: Env,
    tmp_path: Path,
    fakeuni_transport: httpx.ASGITransport,
    fakeuni_cookies: dict[str, str],
) -> None:
    client, _user, thread, space_id, source_id = await _student(env)
    provider = FlowProvider(_script(source_id, space_id, []))
    job_id = await _start(
        env,
        client,
        thread,
        provider,
        "Подключи расписание из кабинета",
        transport=fakeuni_transport,
        cookies=fakeuni_cookies,
        tmp_path=tmp_path,
    )

    seen: set[str] = set()
    await _answer_next(client, thread, job_id, YES, seen)
    await _answer_next(client, thread, job_id, LATER, seen)  # «Не сейчас» on the panel
    job = await env.wait_for(job_id, {"done"}, within=60)
    assert job.result is not None

    async with env.sm() as session:
        custom = await session.scalar(select(Panel).where(Panel.key == "grades-table"))
    assert custom is not None
    assert (custom.status, custom.visible) == ("dismissed", False)
    # Declining the panel does not undo the data the student agreed to: only the card is gone.
    assert await _count(env, Grade, source_id) > 0
    assert "отказался" in _tool_text(provider)


# --- secrets -------------------------------------------------------------------------------


async def test_a_secret_the_broker_issued_never_reaches_a_panel_or_the_trace(
    env: Env, tmp_path: Path
) -> None:
    client, user, thread, space_id, _source_id = await _student(env)

    async def respond(request: LlmRequest) -> LlmResponse:
        step = _steps(request)
        if step == 0:
            return tool_response(call("secret_request", {"question": "Пароль от кабинета?"}, "c1"))
        if step == 1:
            return tool_response(
                call(
                    "ui_propose_panel",
                    {
                        "space_id": space_id,
                        "key": "grades-pass",
                        "type": "kpi",
                        "title": f"Оценки {SECRET}",
                        "dataset": {"kind": "grades"},
                        "rationale": f"Пароль {SECRET}",
                    },
                    "c2",
                )
            )
        return answer("Панель не добавил: в названии оказался секрет.")

    provider = FlowProvider(respond)
    job_id = await _start(env, client, thread, provider, "Покажи оценки", tmp_path=tmp_path)
    await env.wait_for(job_id, {"waiting_user"}, within=30)
    blocks = [
        e.data
        for e in await env.events(job_id)
        if e.type == "block" and e.data.get("type") == "input"
    ]
    posted = await client.post(
        f"/api/v1/jobs/{job_id}/answers", json={"blockId": blocks[-1]["id"], "values": SECRET}
    )
    assert posted.status_code == 204, posted.text
    job = await env.wait_for(job_id, {"done"}, within=30)

    assert "secret_in_text" in _tool_text(provider)
    assert SECRET not in _tool_text(provider)
    assert SECRET not in _dumped([event.data for event in await env.events(job_id)])
    assert SECRET not in _dumped(job.result)
    async with env.sm() as session:
        assert not list(await session.scalars(select(Panel).where(Panel.user_id == user)))
        payloads = list(
            await session.scalars(
                select(AgentRunEvent.payload)
                .join(AgentRun, AgentRun.run_id == AgentRunEvent.run_id)
                .where(AgentRun.user_id == user)
            )
        )
    assert SECRET not in _dumped(payloads)


# --- prompts -------------------------------------------------------------------------------


def test_the_chat_prompt_carries_the_discovery_paragraph() -> None:
    flat = " ".join(CHAT_SYSTEM_PROMPT.split())
    for phrase in (
        "Находки в источнике и панели «Успеваемость»",
        "проверь настоящим чтением",
        "ui_propose_panel",
        "panel_list",
        "Отказ ничего не меняет",
        "не дубли системных",
        "пароли, коды и ключи не клади в название",
        "не усредняй",
        "panel_list, ui_propose_panel, panel_update",
    ):
        assert phrase in flat, phrase
