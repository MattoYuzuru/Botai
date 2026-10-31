"""The `action_*` tools: the surface the v4 runtime hands to the model (D-006)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from botai.actions.engine import ActionEngine
from botai.actions.models import ActionRequest
from botai.agent.v4.protocol import AskAnswer, AskQuestion, ToolContext, ToolResult
from botai.agent.v4.tools.actions import build_action_tools
from tests.actions.conftest import RUN_ID, WRITE_URL, deep_writes
from tests.actions.support import allow
from tests.agent.conftest import Api

pytestmark = pytest.mark.db


def arguments(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "transport": "http",
        "intent": "Подать заявку на справку об обучении",
        "method": "POST",
        "url": WRITE_URL,
        "body": {"language": "ru", "student_id": "S-12345"},
        "body_kind": "form",
    }
    payload.update(overrides)
    return payload


def context(user_id: str, ask: object = None) -> ToolContext:
    return ToolContext(run_id=RUN_ID, user_id=user_id, ask=ask)  # type: ignore[arg-type]


def tools(api: Api, transport: httpx.ASGITransport) -> dict[str, Any]:
    built = build_action_tools(services=api.services, user_id=api.user_id, http_transport=transport)
    return {tool.spec.name: tool for tool in built}


def test_tool_set_and_specs(api: Api, transport: httpx.ASGITransport) -> None:
    built = tools(api, transport)
    assert sorted(built) == ["action_execute", "action_history", "action_propose"]
    execute = built["action_execute"].spec
    assert execute.policy == "write_soft"
    assert execute.serial_only is True
    assert execute.idempotent is True
    assert built["action_propose"].spec.policy == "read"
    assert built["action_history"].spec.policy == "read"


async def test_propose_shows_the_request_and_sends_nothing(
    api: Api, transport: httpx.ASGITransport
) -> None:
    built = tools(api, transport)
    result = await built["action_propose"].run(arguments(), context(api.user_id))
    assert result.ok, result
    assert deep_writes() == 0
    data = result.data
    assert data["policy"]["mode"] == "ask"
    assert data["duplicate"] is False
    assert data["proposal"]["method"] == "POST"
    assert data["proposal"]["url"] == WRITE_URL
    assert data["proposal"]["header_names"] == []
    fields = {item["name"]: item["value"] for item in data["proposal"]["fields"]}
    assert fields["student_id"] == "S-12345"

    history = await built["action_history"].run({}, context(api.user_id))
    assert history.ok
    assert history.data["count"] == 1
    assert history.data["actions"][0]["decision"] == "asked"


async def test_execute_auto_sends_once_and_reports_the_journal(
    action_engine: ActionEngine, api: Api, transport: httpx.ASGITransport
) -> None:
    await allow(action_engine, api.user_id)
    built = tools(api, transport)
    result = await built["action_execute"].run(arguments(), context(api.user_id))
    assert result.ok, result
    assert deep_writes() == 1
    data = result.data
    assert data["decision"] == "executed"
    assert data["mode"] == "auto"
    assert data["request"]["body_sha256"]
    assert data["execution"]["status"] == 200
    assert data["action_id"] > 0


async def test_execute_asks_through_the_tool_context(
    api: Api, transport: httpx.ASGITransport
) -> None:
    asked: list[AskQuestion] = []

    async def ask(question: AskQuestion) -> AskAnswer:
        asked.append(question)
        return AskAnswer(choice=0)

    built = tools(api, transport)
    result = await built["action_execute"].run(arguments(), context(api.user_id, ask=ask))
    assert result.ok, result
    assert deep_writes() == 1
    assert len(asked) == 1
    assert WRITE_URL in asked[0].question


async def test_execute_denied_by_owner_returns_a_structured_failure(
    api: Api, transport: httpx.ASGITransport
) -> None:
    async def ask(_question: AskQuestion) -> AskAnswer:
        return AskAnswer(choice=1)

    built = tools(api, transport)
    result = await built["action_execute"].run(arguments(), context(api.user_id, ask=ask))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "action_denied"
    assert deep_writes() == 0


async def test_execute_without_an_ask_channel_reports_approval_required(
    api: Api, transport: httpx.ASGITransport
) -> None:
    built = tools(api, transport)
    result = await built["action_execute"].run(arguments(), context(api.user_id))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "approval_required"
    assert result.data["decision"] == "asked"
    assert deep_writes() == 0


async def test_never_policy_is_a_policy_failure(
    action_engine: ActionEngine, api: Api, transport: httpx.ASGITransport
) -> None:
    await allow(action_engine, api.user_id, mode="never")
    built = tools(api, transport)
    result = await built["action_execute"].run(arguments(), context(api.user_id))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "policy_denied"
    assert result.data["decision"] == "never"
    assert deep_writes() == 0


async def test_invalid_arguments_are_refused_before_anything(
    api: Api, transport: httpx.ASGITransport
) -> None:
    built = tools(api, transport)
    result = await built["action_execute"].run(
        {"transport": "http", "intent": "Подать заявку"}, context(api.user_id)
    )
    assert isinstance(result, ToolResult)
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "invalid_arguments"
    assert deep_writes() == 0


async def test_propose_then_execute_the_same_key(api: Api, transport: httpx.ASGITransport) -> None:
    built = tools(api, transport)
    keyed = arguments(idempotency_key="spravka-handoff-1")
    proposed = await built["action_propose"].run(keyed, context(api.user_id))
    assert proposed.ok, proposed

    async def ask(_question: AskQuestion) -> AskAnswer:
        return AskAnswer(choice=0)

    executed = await built["action_execute"].run(keyed, context(api.user_id, ask=ask))
    assert executed.ok, executed
    assert executed.data["action_id"] == proposed.data["action_id"]
    assert deep_writes() == 1


async def test_history_never_returns_secret_values(
    action_engine: ActionEngine, api: Api, transport: httpx.ASGITransport
) -> None:
    await allow(action_engine, api.user_id)
    request = ActionRequest.model_validate(
        {
            "transport": "http",
            "intent": "Отправить заявку",
            "method": "POST",
            "url": WRITE_URL,
            "headers": {"Authorization": "Bearer TOOL-SECRET-SENTINEL"},
            "cookies": {"session": "TOOL-COOKIE-SENTINEL"},
            "body": {"password": "TOOL-PASSWORD-SENTINEL", "language": "ru"},
            "body_kind": "form",
        }
    )
    report = await action_engine.run(user_id=api.user_id, request=request, run_id=RUN_ID)
    assert report.status == "executed"

    built = tools(api, transport)
    history = await built["action_history"].run({}, context(api.user_id))
    assert history.ok
    blob = json.dumps(history.data, ensure_ascii=False)
    for sentinel in (
        "TOOL-SECRET-SENTINEL",
        "TOOL-COOKIE-SENTINEL",
        "TOOL-PASSWORD-SENTINEL",
    ):
        assert sentinel not in blob
    row = history.data["actions"][0]
    assert row["request"]["body_sha256"] == request.body_sha256()
