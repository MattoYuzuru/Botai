"""Learning diagnostics describe decisions and losses without recording source content."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from botai.core.browser import FakeBrowserGateway, FakePage, ObservedRequest
from botai.core.diagnostics import configure_diagnostics, diagnostic_scope
from botai.core.llm import LlmResponse, ScriptedLlm
from botai.learn import author, canary, explorer, observe
from botai.learn.lint import LintContext
from botai.learn.redact import Redactor
from botai.settings import Settings
from tests.learn import support as ls
from tests.learn.conftest import EnvFactory
from tests.sources import recipes
from tests.sources.helpers import NOW

ORIGIN = "https://fakeuni.test"
PRIVATE = "PRIVATE-FIXTURE-CONTENT"
Event = tuple[str, dict[str, object]]


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[Event]:
    captured: list[Event] = []

    def emit(name: str, **facts: object) -> None:
        captured.append((name, facts))

    for module in (author, canary, explorer, observe):
        monkeypatch.setattr(module, "diagnostic_event", emit)
    return captured


def matching(events: list[Event], name: str) -> list[dict[str, object]]:
    return [facts for event, facts in events if event == name]


async def test_observation_reports_auth_page_and_missing_sample_without_content(
    events: list[Event],
) -> None:
    url = f"{ORIGIN}/schedule?week={PRIVATE}"
    gateway = FakeBrowserGateway(
        {
            url: FakePage(
                f"<main><h2>Вы не вошли в аккаунт</h2><p>{PRIVATE}</p></main>",
                title=PRIVATE,
                requests=[
                    ObservedRequest(
                        method="GET",
                        url=f"{ORIGIN}/api/records?student={PRIVATE}",
                        status=200,
                        resource_type="xhr",
                        response_content_type="application/json",
                        response_sample=None,
                    )
                ],
            )
        },
        NOW,
        ORIGIN,
    )
    handle = await gateway.open(
        user_id="fixture-user", connection_id="fixture-connection", allow_hosts=["fakeuni.test"]
    )
    result = await observe.observe(handle, ORIGIN, Redactor(), start_urls=[url])
    await handle.close()
    assert len(result.pages) == len(result.calls) == 1
    page = next(row for row in matching(events, "observe.page") if row["phase"] == "final")
    assert page["login_required"] is True
    assert page["redirected"] is False
    assert matching(events, "observe.complete")[0]["sample_missing_count"] == 1
    assert matching(events, "observe.call")[0]["status"] == 200
    assert PRIVATE not in json.dumps(events)
    assert "/schedule" not in json.dumps(events)
    assert "/api/records" not in json.dumps(events)
    assert "Вы не вошли" not in json.dumps(events)


async def test_observation_failed_navigation_and_render_budget_are_distinct(
    events: list[Event],
) -> None:
    gateway = FakeBrowserGateway({}, NOW, ORIGIN)
    handle = await gateway.open(
        user_id="fixture-user", connection_id="fixture-connection", allow_hosts=["fakeuni.test"]
    )
    result = await observe.observe(
        handle, ORIGIN, Redactor(), start_urls=[f"{ORIGIN}/private/{PRIVATE}"]
    )
    assert result.empty
    assert matching(events, "observe.visit_failed")[0]["reason"] == "navigation_failed"
    assert matching(events, "observe.complete")[0]["failed_count"] == 1
    obs = observe.Observations(
        origin=ORIGIN,
        pages=[observe.PageView("/records", PRIVATE * 20, [], "<main>" + PRIVATE * 20 + "</main>")],
    )
    assert "observation budget exceeded" in obs.render(limit=60)
    assert matching(events, "observe.render")[0]["outcome"] == "budget_exceeded"
    assert PRIVATE not in json.dumps(events)


async def test_author_diagnostics_separate_invalid_json_and_no_candidates(
    events: list[Event],
) -> None:
    llm = ScriptedLlm(
        queue=[LlmResponse(content=PRIVATE), ls.answer(None, reasoning=PRIVATE, doubts=[PRIVATE])]
    )
    result = await author.author_kind(
        llm,
        "scheduleEvents",
        PRIVATE,
        lint_ctx=LintContext(),
        origin=ORIGIN,
        revision=1,
        now=NOW,
        digest="fixture",
    )
    assert result.calls == 2
    rows = matching(events, "learn.author")
    assert any(row.get("reason") == "invalid_json" for row in rows)
    assert any(row.get("outcome") == "not_found" and row.get("found") is False for row in rows)
    assert PRIVATE not in json.dumps(events)


async def test_successful_learning_explains_lint_retry_canary_and_selection(
    make_env: EnvFactory, events: list[Event]
) -> None:
    leaky = recipes.timetable_endpoint()
    leaky["request"]["query"]["owner"] = "private.student@fakeuni.test"
    env = await make_env(
        [ls.answer([leaky], reasoning=PRIVATE), ls.timetable_answer(), ls.deadlines_answer()]
    )
    result = await env.run()
    assert result["status"] == "ok"
    assert any(row.get("lint_code") == "personal.email" for row in matching(events, "learn.lint"))
    assert matching(events, "canary.complete")
    selected = matching(events, "learn.selection")
    assert {row.get("kind") for row in selected if row["outcome"] == "accepted"} == {
        "scheduleEvents",
        "deadlines",
    }
    assert matching(events, "learn.confirmation")[0]["confirmed"] is True
    assert matching(events, "learn.outcome")[-1]["outcome"] == "ok"
    dump = json.dumps(events)
    for private in (PRIVATE, "private.student@fakeuni.test", "Иванова", "u-1001"):
        assert private not in dump


async def test_learning_trace_is_persisted_with_correlation_and_structural_facts(
    make_env: EnvFactory, tmp_path: Path
) -> None:
    job_id = "01M61YP9N7N1HHQRCCVJRGS74K"
    thread_id = "01M61JYBYEVF5H75AQB8YB69F4"
    leaky = recipes.timetable_endpoint()
    leaky["request"]["query"]["owner"] = "private.student@fakeuni.test"
    env = await make_env(
        [ls.answer([leaky], reasoning=PRIVATE), ls.timetable_answer(), ls.deadlines_answer()],
        home_extra=PRIVATE,
    )

    def read_records() -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for path in sorted(tmp_path.glob("*.jsonl*"))
            for line in path.read_text().splitlines()
        ]

    configure_diagnostics(
        Settings(
            botai_diagnostics=True,
            botai_diagnostics_dir=str(tmp_path),
            botai_diagnostics_max_bytes=1_000_000,
        )
    )
    try:
        with diagnostic_scope(job_id=job_id, thread_id=thread_id):
            result = await env.run()
        assert result["status"] == "ok"
        rows = await asyncio.to_thread(read_records)
    finally:
        configure_diagnostics(
            Settings(botai_diagnostics=False, botai_diagnostics_dir=str(tmp_path))
        )
    assert rows
    assert all(row["job_id"] == job_id and row["thread_id"] == thread_id for row in rows)
    names = [row["event"] for row in rows]
    ordered = [
        "observe.start",
        "observe.complete",
        "learn.author",
        "learn.lint",
        "canary.start",
        "canary.complete",
        "learn.selection",
        "learn.confirmation",
        "learn.outcome",
    ]
    assert [names.index(name) for name in ordered] == sorted(names.index(name) for name in ordered)
    observed = next(row for row in rows if row["event"] == "observe.complete")
    assert observed["page_count"] > 0
    assert observed["call_count"] > 0
    assert observed["considered_count"] >= observed["call_count"]
    lint = next(row for row in rows if row.get("lint_code") == "personal.email")
    assert lint["kind"] == "scheduleEvents"
    assert lint["outcome"] == "rejected"
    selected = [row for row in rows if row["event"] == "learn.selection"]
    assert {row["kind"] for row in selected} == {"scheduleEvents", "deadlines"}
    assert all(row["outcome"] == "accepted" and row["record_count"] > 0 for row in selected)
    assert names[-1] == "learn.outcome"
    assert rows[-1]["outcome"] == "ok"
    dump = json.dumps(rows)
    for private in (PRIVATE, "private.student@fakeuni.test", "Иванова", "u-1001", "/api/v2/"):
        assert private not in dump
