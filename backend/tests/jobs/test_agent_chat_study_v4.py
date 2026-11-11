"""v4 CHAT answers study questions from BotAI's own data (D-044), through the real worker.

The production audit of 2026-11-11: «мне поставили оценку за ДЗ 7?» took 15–109 model steps and up
to 20 minutes, because the chat had no tool for the data BotAI already synced and launched a
browser on every turn. These tests drive API → worker → v4 harness with a scripted model and the
real tools:

* the system prompt carries the fast-answer rules and the study digest; the grade question is
  answered with one `study_data` call, and no browser is launched although the turn has one;
* a stale source is synced first (`source_sync`, deterministic playbook, compact report with its
  warnings), then `study_data` returns the fresh rows with the source's new age.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from botai.agent.v4 import factory
from botai.agent.v4.chat import CORRECTION_FLOW, FAST_ANSWERS, make_chat_handlers
from botai.core.llm import LlmRequest, LlmResponse
from botai.db.models import Grade, Source
from botai.sources.v4 import playbooks
from botai.sources.v4 import service as sources_service
from botai.sources.v4.schemas import SourcePatch
from tests.agent.v4.runtime_support import answer, call, tool_response
from tests.auth.support import NOW
from tests.fakeuni.login import COOKIE, SESSIONS
from tests.jobs.env import Env
from tests.jobs.test_agent_chat_v4 import FlowProvider, _last_message, _post, _steps, _student
from tests.panels.support import BASE
from tests.panels.test_sync_and_cleanup import SPEC as CABINET_SPEC

pytestmark = pytest.mark.db

GRADES_SPEC: dict[str, Any] = {
    **CABINET_SPEC,
    "reads": [read for read in CABINET_SPEC["reads"] if read["kind"] == "grades"],
}


class Launches:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self, services: Any, **kwargs: Any) -> Any:
        del services, kwargs
        self.count += 1
        raise AssertionError("a study answer must not launch the browser")


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> Launches:
    counter = Launches()
    monkeypatch.setattr(factory, "launch_browser_control", counter)
    return counter


def _text(request: LlmRequest) -> str:
    return "\n".join(message.content for message in request.messages)


def _tool_results(request: LlmRequest) -> list[str]:
    return [message.content for message in request.messages if message.role == "tool"]


def _worker(env: Env, provider: FlowProvider, tmp_path: Path, **kwargs: Any) -> Any:
    env.settings.agent_engine = "v4"
    # `with_browser` is left to the chat's default: the turn has a browser, lazily.
    return env.worker(
        make_chat_handlers(provider=provider, artifact_root=str(tmp_path / "artifacts"), **kwargs)
    )


async def _source(env: Env, user: str, space: str, *, playbook: bool, synced_ago: timedelta) -> str:
    async with env.sm() as session:
        source = await sources_service.create_source(
            session,
            user_id=user,
            space_id=space,
            title="ЛК Fakeuni",
            entry_url=f"{BASE}/app",
            kinds=["site", "grades"],
        )
        if playbook:
            book = await playbooks.create_playbook(session, user_id=user, title="Fakeuni")
            version = await playbooks.add_playbook_version(
                session, user_id=user, playbook_id=book.id, spec=GRADES_SPEC
            )
            await playbooks.record_canary(
                session,
                user_id=user,
                playbook_id=book.id,
                version=version.version,
                canary={"ok": True},
            )
            await playbooks.activate_playbook_version(
                session, user_id=user, playbook_id=book.id, version=version.version
            )
            await sources_service.update_source(
                session, user_id=user, source_id=source.id, patch=SourcePatch(playbook_id=book.id)
            )
        await session.execute(
            update(Source)
            .where(Source.id == source.id)
            .values(status="active", last_sync_at=NOW - synced_ago)
        )
        await session.commit()
        return source.id


async def test_a_grade_question_is_one_study_data_call_and_no_browser(
    env: Env, tmp_path: Path, launches: Launches
) -> None:
    client, user, thread, space = await _student(env, with_space=True)
    assert space is not None
    source_id = await _source(env, user, space, playbook=False, synced_ago=timedelta(minutes=5))
    async with env.sm() as session:
        session.add(
            Grade(
                user_id=user,
                source_id=source_id,
                external_id="hw-7",
                source_url=f"{BASE}/api",
                fetched_at=NOW,
                seen_at=NOW,
                title="ДЗ 7",
                value="9",
                value_num=9,
                scale_max=10,
                layer="item",
                url=f"{BASE}/learn/tasks/7",
                graded_at=NOW - timedelta(hours=3),
            )
        )
        await session.commit()

    async def respond(request: LlmRequest) -> LlmResponse:
        if _steps(request) == 0:
            return tool_response(call("study_data", {"kinds": ["grades"], "query": "ДЗ 7"}, "s1"))
        return answer(
            f"Да: за ДЗ 7 — 9 из 10 ({BASE}/learn/tasks/7), данные обновлены 5 мин назад."
        )

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "Мне поставили оценку за ДЗ 7?")
    env.start(_worker(env, provider, tmp_path))
    job = await env.wait_for(sent.json()["jobId"], {"done"}, within=30)

    assert job.result is not None
    assert job.result["stopped"] == "answer"
    assert job.result["tools"] == ["study_data"]
    assert job.result["steps"] == 2
    assert launches.count == 0, "the browser is lazy: nothing used it, nothing launched it"

    first = provider.requests[0]
    names = {tool.name for tool in first.tools}
    assert {"study_data", "study_status_rule", "study_mark_task", "browser_navigate"} <= names
    prompt = _text(first)
    assert FAST_ANSWERS in prompt
    assert CORRECTION_FLOW in prompt
    assert "Сводка учёбы" in prompt
    assert f"source_id={source_id}" in prompt
    assert "обновлено 5 мин назад" in prompt

    (result,) = _tool_results(provider.requests[1])
    assert '"value": "9"' in result
    assert f"{BASE}/learn/tasks/7" in result
    reply = await _last_message(env, thread)
    assert reply is not None
    assert "9 из 10" in reply.blocks[0]["markdown"]


async def test_a_stale_source_is_synced_before_the_answer(
    env: Env, tmp_path: Path, launches: Launches, fakeuni_app: Any
) -> None:
    client, user, thread, space = await _student(env, with_space=True)
    assert space is not None
    source_id = await _source(env, user, space, playbook=True, synced_ago=timedelta(hours=2))
    token = "study-fixture-session"
    SESSIONS.add(token)

    async def respond(request: LlmRequest) -> LlmResponse:
        steps = _steps(request)
        if steps == 0:
            # The digest says how old the data are: older than 15 minutes means sync first.
            assert "обновлено 2 ч назад" in _text(request)
            return tool_response(call("source_sync", {"source_id": source_id}, "s1"))
        if steps == 1:
            return tool_response(call("study_data", {"kinds": ["grades"]}, "s2"))
        return answer("Обновил кабинет: новые оценки есть.")

    provider = FlowProvider(respond)
    sent = await _post(client, thread, "Появились новые оценки?")
    env.start(
        _worker(
            env,
            provider,
            tmp_path,
            http_transport=httpx.ASGITransport(app=fakeuni_app),
            source_cookies={COOKIE: token},
        )
    )
    job = await env.wait_for(sent.json()["jobId"], {"done"}, within=30)

    assert job.result is not None
    assert job.result["stopped"] == "answer"
    assert job.result["tools"] == ["source_sync", "study_data"]
    assert launches.count == 0

    (sync_text,) = _tool_results(provider.requests[1])
    sync = json.loads(sync_text[sync_text.index("{") : sync_text.rindex("}") + 1])
    report = sync["data"]["report"] if "data" in sync else sync["report"]
    assert report["ok"] is True
    assert report["counts"]["grades"] > 0
    assert isinstance(report["records"], int), "a chat sync returns counts, not the raw records"
    assert "warnings" in (sync.get("data") or sync)

    study_text = _tool_results(provider.requests[2])[-1]
    assert "Математический анализ" in study_text
    assert '"ageMinutes": 0' in study_text, "the source was just synced"
