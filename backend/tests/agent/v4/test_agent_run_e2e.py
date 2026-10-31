"""End-to-end experimental path: runtime + real Chromium + executor on a loopback cabinet.

Mock provider scripts the decisions; the tools run for real: `browser_*` drives Chromium,
`code_exec` runs Python in the run workspace, `fs_read` reads what the code wrote. The trace is a
`MemoryTraceStore` here; the DB adapter has its own tests (tests/jobs/test_agent_v4.py).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from botai.agent.v4.factory import build_agent_wiring, memory_trace_store, run_limits_from_settings
from botai.agent.v4.handlers import SYSTEM_PROMPT
from botai.agent.v4.providers import MockProvider
from botai.agent.v4.runtime import RunTask
from tests.agent.v4.agent_wiring_support import fake_services, fixture_settings, serve_agent_site
from tests.agent.v4.runtime_support import answer, call, tool_response

pytestmark = pytest.mark.browser


@pytest.fixture
async def site() -> AsyncIterator[str]:
    async with serve_agent_site() as base:
        yield base


def task(task_text: str, workspace: Path) -> RunTask:
    return RunTask(
        run_id="01M68Z000000000000000000B0",
        task=task_text,
        system=SYSTEM_PROMPT,
        workspace=str(workspace),
        user_id="u1",
    )


async def test_agent_opens_the_cabinet_saves_the_schedule_and_reports(
    site: str, tmp_path: Path
) -> None:
    services = fake_services()
    store = memory_trace_store()
    provider = MockProvider(
        responses=[
            tool_response(call("browser_navigate", {"url": f"{site}/cabinet"}, "c1")),
            tool_response(call("browser_read", {"mode": "text"}, "c2")),
            tool_response(
                call(
                    "code_exec",
                    {
                        "language": "python",
                        "code": (
                            "rows = ['Математический анализ', 'Базы данных']\n"
                            "open('schedule.txt', 'w', encoding='utf-8').write('\\n'.join(rows))\n"
                        ),
                    },
                    "c3",
                )
            ),
            tool_response(call("fs_read", {"path": "schedule.txt"}, "c4")),
            answer("Расписание найдено (2 пары) и сохранено в schedule.txt"),
        ]
    )
    wiring = await build_agent_wiring(
        services,
        user_id="u1",
        run_id="01M68Z000000000000000000B0",
        workspace_root=tmp_path,
        provider=provider,
        trace_store=store,
        with_browser=True,
    )
    try:
        result = await wiring.runtime.run(
            task("открой кабинет, найди расписание и сохрани результат", wiring.workspace.root),
            provider,
            wiring.tools,
            trace_store=store,
            limits=run_limits_from_settings(services.settings),
        )
    finally:
        await wiring.aclose()

    assert result.status == "answer"
    assert "schedule.txt" in result.content
    assert result.steps == 5
    assert result.tool_calls == 4

    # The schedule came from the page and the code wrote a real file in the same workspace.
    saved = (wiring.workspace.root / "schedule.txt").read_text(encoding="utf-8")
    assert "Математический анализ" in saved
    assert "Базы данных" in saved

    trace = await store.get_run(result.run_id)
    assert trace is not None
    kinds = [event.kind for event in trace.events]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert kinds.count("tool_call") == 4
    assert kinds.count("model_call") == 5
    read_results = [
        event.payload
        for event in trace.events
        if event.kind == "tool_result" and event.payload.get("tool") == "browser_read"
    ]
    assert read_results
    assert "Математический анализ" in json.dumps(read_results, ensure_ascii=False)
    finished = trace.events[-1].payload
    assert finished["status"] == "answer"
    assert finished["steps"] == 5


async def test_the_soft_step_limit_stops_a_resumable_run(site: str, tmp_path: Path) -> None:
    settings = fixture_settings(agent_limits_steps=1)
    services = fake_services(settings=settings)
    store = memory_trace_store()
    provider = MockProvider(
        responses=[
            tool_response(call("browser_navigate", {"url": f"{site}/cabinet"}, "c1")),
            answer("не должен быть вызван"),
        ]
    )
    wiring = await build_agent_wiring(
        services,
        user_id="u1",
        run_id="01M68Z000000000000000000B1",
        workspace_root=tmp_path,
        provider=provider,
        trace_store=store,
        with_browser=True,
    )
    try:
        result = await wiring.runtime.run(
            task("найди расписание", wiring.workspace.root),
            provider,
            wiring.tools,
            trace_store=store,
            limits=run_limits_from_settings(settings),
        )
    finally:
        await wiring.aclose()

    assert result.status == "step_limit"
    assert result.limit_reached == "steps"
    assert result.can_resume
    assert not result.answered
    trace = await store.get_run(result.run_id)
    assert trace is not None
    assert any(event.kind == "limit" for event in trace.events)
    assert provider.requests
    assert len(provider.requests) == 1


async def test_a_run_without_browser_still_has_code_and_files(tmp_path: Path) -> None:
    services = fake_services()
    store = memory_trace_store()
    provider = MockProvider(
        responses=[
            tool_response(
                call(
                    "code_exec",
                    {
                        "language": "python",
                        "code": "open('notes.txt', 'w').write('hello')",
                    },
                    "c1",
                )
            ),
            tool_response(call("fs_read", {"path": "notes.txt"}, "c2")),
            answer("готово"),
        ]
    )
    wiring = await build_agent_wiring(
        services,
        user_id="u1",
        run_id="01M68Z000000000000000000B2",
        workspace_root=tmp_path,
        provider=provider,
        trace_store=store,
        with_browser=False,
    )
    try:
        result = await wiring.runtime.run(
            task("сохрани заметку", wiring.workspace.root),
            provider,
            wiring.tools,
            trace_store=store,
        )
    finally:
        await wiring.aclose()

    assert result.status == "answer"
    assert (wiring.workspace.root / "notes.txt").read_text() == "hello"


async def test_tool_names_of_the_wiring_are_stable(tmp_path: Path) -> None:
    # No browser launch: names are fixed by the wiring, documented in docs/v4/15-runtime-wiring.md.
    wiring = await build_agent_wiring(
        fake_services(),
        user_id="u1",
        run_id="01M68Z000000000000000000B3",
        provider=MockProvider(),
        trace_store=memory_trace_store(),
        with_browser=False,
        workspace_root=tmp_path,
    )
    try:
        tool_names = {tool.spec.name for tool in wiring.tools}
    finally:
        await wiring.aclose()
    assert {"code_exec", "secret_request", "secret_use", "fs_read", "http_request"} <= tool_names
