"""Regression: real lessons must not become a successful, empty parser canary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from botai.agent.v4.executor import ExecResult
from botai.agent.v4.protocol import ToolContext
from botai.agent.v4.tools.sources import SourceToolkit
from botai.sources.adapter import Record
from botai.sources.normalize import coerce
from botai.sources.v4 import service
from botai.sources.v4.agent_schema import source_schema
from botai.sources.v4.runner import SourceRunner
from tests.auth.support import Sm
from tests.sources.v4.support import Account


class OutputExecutor:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output

    async def run(self, request: Any) -> ExecResult:
        return ExecResult(stdout=json.dumps(self.output), stderr="", exit_code=0, duration_ms=1)

    def close(self) -> None:
        pass


def test_agent_schema_examples_are_accepted_by_the_actual_normalizer() -> None:
    schema = source_schema()
    assert "startsAt" in schema["fields"]["schedule"]["required"]
    assert "dueAt" in schema["fields"]["deadlines"]["required"]
    assert "empty_reason" in schema["parser_output_schema"]["properties"]
    for row in schema["example"]["output"]["records"]:
        kind = "scheduleEvents" if row["kind"] == "schedule" else row["kind"]
        assert (
            coerce(
                kind,
                Record(
                    kind=kind, external_id=row["external_id"], fields=row["fields"], source_url=""
                ),
                ZoneInfo("Europe/Moscow"),
            )
            is not None
        )


@pytest.mark.db
@pytest.mark.parametrize(
    ("fields", "accepted"),
    [
        ({"title": "Математика", "date": "2026-11-02", "begin": "10:00", "end": "11:30"}, False),
        ({"title": "Математика", "starts_at": "2026-11-02T10:00:00+03:00"}, False),
        (
            {
                "title": "Математика",
                "startsAt": "2026-11-02T10:00:00+03:00",
                "endsAt": "2026-11-02T11:30:00+03:00",
            },
            True,
        ),
    ],
)
async def test_parser_invalid_fields_are_actionable_and_never_activate(
    tx_sessionmaker: Sm, account: Account, tmp_path: Path, fields: dict[str, Any], accepted: bool
) -> None:
    async with tx_sessionmaker() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title="Кабинет",
            entry_url="https://fakeuni.test/app",
            kinds=[],
        )
        await session.commit()
    toolkit = SourceToolkit(
        sm=tx_sessionmaker,
        executor_factory=lambda: OutputExecutor(
            {"records": [{"kind": "schedule", "external_id": "lesson-1", "fields": fields}]}
        ),
    )
    tools = {tool.spec.name: tool for tool in toolkit.tools()}
    ctx = ToolContext(run_id="run", task="Подключи", user_id=account.user_id, workspace=tmp_path)
    result = await tools["source_update_playbook"].run(
        {"source_id": source.id, "code": "print('parser')", "activate": True}, ctx
    )
    assert result.ok is accepted
    assert result.data["activated"] is accepted
    canary = result.data["canary"]
    assert canary["ok"] is accepted
    if accepted:
        assert canary["counts"] == {"schedule": 1}
    else:
        assert canary["counts"] == {}
        assert canary["errors"][0]["code"] == "recipe_drift"
        assert "startsAt" in canary["errors"][0]["detail"]
        assert "source_schema" in canary["errors"][0]["detail"]


@pytest.mark.db
@pytest.mark.parametrize(
    ("output", "ok"),
    [
        ({}, False),
        ({"records": []}, False),
        ({"records": [], "empty_reason": " "}, False),
        ({"records": [], "empty_reason": "Проверенный API вернул пустой список за период"}, True),
    ],
)
async def test_parser_empty_data_requires_an_explicit_reason(
    tx_sessionmaker: Sm, account: Account, output: dict[str, Any], ok: bool
) -> None:
    async with tx_sessionmaker() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title="Кабинет",
            entry_url="https://fakeuni.test/app",
            kinds=[],
        )
        report = await SourceRunner(
            session, user_id=account.user_id, executor_factory=lambda: OutputExecutor(output)
        ).probe(source.id, code="print('parser')")
    assert report.ok is ok
    if not ok:
        assert report.errors[0].code == "recipe_drift"
        assert "empty_reason" in report.errors[0].detail
