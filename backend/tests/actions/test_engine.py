"""The action engine on the synthetic university: auto/ask/never, journal, idempotency, masking.

Every execution here is a real HTTP request through the ASGI transport to `fakeuni` (L1): the
counter `fakeuni.deep.STATE.writes` proves whether a request went out, the `actions_log` row
proves what the journal keeps.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from botai.actions.engine import ActionEngine
from botai.actions.models import ActionRequest
from botai.agent.v4.protocol import AskAnswer, AskQuestion
from botai.db.models import ActionLog
from tests.actions.conftest import ORIGIN, RUN_ID, WRITE_URL, deep_writes
from tests.actions.support import allow, write_request
from tests.agent.conftest import Api

pytestmark = pytest.mark.db

SENTINEL_HEADER = "Bearer SUPER-SECRET-TOKEN-42"
SENTINEL_COOKIE = "COOKIE-SENTINEL-42"
SENTINEL_PASSWORD = "P@ssw0rd-SENTINEL-42"


async def test_auto_action_executes_once_and_is_journaled(
    action_engine: ActionEngine, api: Api
) -> None:
    await allow(action_engine, api.user_id)
    request = write_request()
    report = await action_engine.run(user_id=api.user_id, request=request, run_id=RUN_ID)

    assert report.status == "executed"
    assert report.entry.decision == "executed"
    assert report.entry.mode == "auto"
    assert report.entry.action_class == "write_soft"
    assert report.entry.scope_kind == "global"
    assert deep_writes() == 1
    summary = report.entry.request
    assert summary is not None
    assert (summary["transport"], summary["method"], summary["url"]) == (
        "http",
        "POST",
        WRITE_URL,
    )
    assert summary["body_kind"] == "form"
    assert summary["body_bytes"] > 0
    assert summary["body_sha256"] == request.body_sha256()
    assert report.entry.result is not None
    assert report.entry.result["ok"] is True
    assert report.entry.result["status"] == 200
    assert report.entry.executed_at is not None
    history = await action_engine.history(user_id=api.user_id)
    assert len(history) == 1


async def test_ask_mode_asks_with_exact_details_then_executes(
    action_engine: ActionEngine, api: Api
) -> None:
    asked: list[AskQuestion] = []

    async def ask(question: AskQuestion) -> AskAnswer:
        asked.append(question)
        return AskAnswer(choice=0)

    report = await action_engine.run(
        user_id=api.user_id, request=write_request(), run_id=RUN_ID, ask=ask
    )

    assert report.status == "executed"
    assert deep_writes() == 1
    assert len(asked) == 1
    question = asked[0].question
    assert asked[0].options == ("Разрешить", "Запретить")
    assert "POST" in question
    assert WRITE_URL in question
    assert "S-12345" in question
    assert "write_soft" in question
    assert report.entry.decision == "executed"
    assert report.entry.decided_at is not None
    assert report.entry.executed_at is not None


async def test_ask_mode_denied_sends_nothing(action_engine: ActionEngine, api: Api) -> None:
    async def ask(_question: AskQuestion) -> AskAnswer:
        return AskAnswer(choice=1)

    report = await action_engine.run(
        user_id=api.user_id, request=write_request(), run_id=RUN_ID, ask=ask
    )

    assert report.status == "denied"
    assert deep_writes() == 0
    assert report.entry.decision == "denied"
    assert report.entry.request is None
    assert await action_engine.history(user_id=api.user_id) == [report.entry]


async def test_ask_mode_cancelled_denies(action_engine: ActionEngine, api: Api) -> None:
    async def ask(_question: AskQuestion) -> AskAnswer:
        return AskAnswer(cancelled=True)

    report = await action_engine.run(
        user_id=api.user_id, request=write_request(), run_id=RUN_ID, ask=ask
    )
    assert report.status == "denied"
    assert deep_writes() == 0


async def test_never_policy_refuses_without_asking(action_engine: ActionEngine, api: Api) -> None:
    await allow(action_engine, api.user_id, mode="never")

    async def unexpected(_question: AskQuestion) -> AskAnswer:  # pragma: no cover
        raise AssertionError("ask must not be called for a never policy")

    report = await action_engine.run(
        user_id=api.user_id, request=write_request(), run_id=RUN_ID, ask=unexpected
    )

    assert report.status == "never"
    assert deep_writes() == 0
    assert report.entry.decision == "never"
    assert report.entry.request is None


async def test_approval_required_then_decide_then_retry_executes_once(
    action_engine: ActionEngine, api: Api
) -> None:
    report = await action_engine.run(
        user_id=api.user_id, request=write_request(), run_id=RUN_ID, ask=None
    )
    assert report.status == "approval_required"
    assert report.entry.decision == "asked"
    assert deep_writes() == 0

    decided = await action_engine.decide(
        user_id=api.user_id, action_id=report.entry.id, approve=True
    )
    assert decided.decision == "approved"

    retried = await action_engine.run(
        user_id=api.user_id, request=write_request(), run_id=RUN_ID, ask=None
    )
    assert retried.status == "executed"
    assert retried.duplicate is True
    assert deep_writes() == 1
    assert len(await action_engine.history(user_id=api.user_id)) == 1


async def test_explicit_idempotency_key_never_sends_twice(
    action_engine: ActionEngine, api: Api
) -> None:
    await allow(action_engine, api.user_id)
    request = write_request(idempotency_key="spravka-2026-1")
    first = await action_engine.run(user_id=api.user_id, request=request, run_id=RUN_ID)
    assert first.status == "executed"
    assert deep_writes() == 1

    # Same key, even with a different task/run id: the journaled result is returned.
    second = await action_engine.run(
        user_id=api.user_id,
        request=request,
        run_id="01M68Z000000000000000000A9",
    )
    assert second.status == "executed"
    assert second.duplicate is True
    assert second.entry.id == first.entry.id
    assert deep_writes() == 1
    assert len(await action_engine.history(user_id=api.user_id)) == 1


async def test_derived_key_deduplicates_retries_inside_one_run(
    action_engine: ActionEngine, api: Api
) -> None:
    await allow(action_engine, api.user_id)
    first = await action_engine.run(user_id=api.user_id, request=write_request(), run_id=RUN_ID)
    second = await action_engine.run(user_id=api.user_id, request=write_request(), run_id=RUN_ID)
    assert first.status == second.status == "executed"
    assert second.duplicate is True
    assert deep_writes() == 1


async def test_a_new_run_may_repeat_the_same_proposal(
    action_engine: ActionEngine, api: Api
) -> None:
    await allow(action_engine, api.user_id)
    first = await action_engine.run(user_id=api.user_id, request=write_request(), run_id=RUN_ID)
    second = await action_engine.run(
        user_id=api.user_id,
        request=write_request(),
        run_id="01M68Z000000000000000000A3",
    )
    assert first.entry.id != second.entry.id
    assert second.duplicate is False
    assert deep_writes() == 2  # a deliberate second submission in another task


async def test_journal_keeps_no_secret_values(action_engine: ActionEngine, api: Api) -> None:
    await allow(action_engine, api.user_id)
    request = ActionRequest.model_validate(
        {
            "transport": "http",
            "intent": "Отправить заявку в деканат",
            "method": "POST",
            "url": WRITE_URL,
            "headers": {"Authorization": SENTINEL_HEADER, "X-Trace": "plain"},
            "cookies": {"session": SENTINEL_COOKIE},
            "body": {
                "password": SENTINEL_PASSWORD,
                "language": "ru",
                "note": "обычное поле",
            },
            "body_kind": "form",
        }
    )
    report = await action_engine.run(user_id=api.user_id, request=request, run_id=RUN_ID)
    assert report.status == "executed"
    assert deep_writes() == 1

    entry = await action_engine.get(user_id=api.user_id, action_id=report.entry.id)
    blob = json.dumps(entry.public(), ensure_ascii=False)
    for sentinel in (SENTINEL_HEADER, SENTINEL_COOKIE, SENTINEL_PASSWORD):
        assert sentinel not in blob
    assert entry.proposal["header_names"] == ["authorization", "x-trace"]
    assert entry.proposal["cookie_names"] == ["session"]
    fields = {item["name"]: item["value"] for item in entry.proposal["fields"]}
    assert fields["password"] == "***"
    assert fields["language"] == "ru"
    assert fields["note"] == "обычное поле"
    assert entry.request is not None
    assert entry.request["body_sha256"] == request.body_sha256()

    # The same is true of the stored row itself, not only of the public projection.
    async with api.sm() as session:
        row = (
            await session.execute(select(ActionLog).where(ActionLog.id == entry.id))
        ).scalar_one()
        stored = json.dumps(
            {"proposal": row.proposal, "request": row.request, "result": row.result},
            ensure_ascii=False,
        )
    for sentinel in (SENTINEL_HEADER, SENTINEL_COOKIE, SENTINEL_PASSWORD):
        assert sentinel not in stored


async def test_a_rejected_site_response_is_journaled_as_failed(
    action_engine: ActionEngine, api: Api
) -> None:
    await allow(action_engine, api.user_id)
    request = write_request(url=f"{ORIGIN}/missing-endpoint")
    report = await action_engine.run(user_id=api.user_id, request=request, run_id=RUN_ID)
    assert report.status == "failed"
    assert report.entry.decision == "failed"
    assert report.entry.result is not None
    assert report.entry.result["status"] == 404
    assert report.entry.request is not None
    assert report.entry.request["url"] == f"{ORIGIN}/missing-endpoint"
    assert deep_writes() == 0


async def test_another_users_journal_is_not_visible(action_engine: ActionEngine, api: Api) -> None:
    from tests.agent.conftest import add_user

    await allow(action_engine, api.user_id)
    report = await action_engine.run(user_id=api.user_id, request=write_request(), run_id=RUN_ID)
    other = await add_user(api.sm, "other")
    assert await action_engine.history(user_id=other) == []
    from botai.core.errors import BotaiError, ErrorCode

    with pytest.raises(BotaiError) as exc:
        await action_engine.get(user_id=other, action_id=report.entry.id)
    assert exc.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(BotaiError):
        await action_engine.decide(user_id=other, action_id=report.entry.id, approve=True)


async def test_engine_uses_the_owner_clock(action_engine: ActionEngine, api: Api) -> None:
    """The engine stamps decisions with the injected clock, not the wall clock."""
    await allow(action_engine, api.user_id)
    report = await action_engine.run(user_id=api.user_id, request=write_request(), run_id=RUN_ID)
    expected = api.services.clock.now()
    assert report.entry.executed_at is not None
    assert report.entry.executed_at.replace(microsecond=0) == expected.replace(microsecond=0)


def test_only_write_methods_can_be_proposed() -> None:
    with pytest.raises(ValidationError):
        ActionRequest.model_validate(
            {"transport": "http", "intent": "test", "method": "GET", "url": WRITE_URL}
        )
