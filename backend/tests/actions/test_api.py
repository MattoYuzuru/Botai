"""The actions API: policy CRUD and the journal/decision flow over the created FastAPI app."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from botai.actions.api import get_action_engine
from botai.actions.engine import ActionEngine
from tests.actions.conftest import ORIGIN, RUN_ID, WRITE_URL, deep_writes
from tests.actions.support import write_request
from tests.agent.conftest import add_user

pytestmark = pytest.mark.db


@pytest.fixture
def api_engine(api: Any, transport: httpx.ASGITransport) -> ActionEngine:
    engine = ActionEngine(
        db=api.sm,
        clock=api.services.clock.now,
        http_transport=transport,
    )
    api.app.dependency_overrides[get_action_engine] = lambda: engine
    return engine


async def test_policy_crud(api: Any, api_engine: ActionEngine) -> None:
    async with api.client() as client:
        created = await client.put(
            "/api/v1/action-policies",
            json={
                "scopeKind": "global",
                "actionClass": "write_soft",
                "mode": "auto",
            },
        )
        assert created.status_code == 200, created.text
        row = created.json()
        assert (row["scopeKind"], row["actionClass"], row["mode"]) == (
            "global",
            "write_soft",
            "auto",
        )

        listed = (await client.get("/api/v1/action-policies")).json()
        assert [item["mode"] for item in listed] == ["auto"]

        updated = await client.put(
            "/api/v1/action-policies",
            json={
                "scopeKind": "global",
                "actionClass": "write_soft",
                "mode": "ask",
                "limits": {"max_per_day": 2},
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["id"] == row["id"]
        assert updated.json()["mode"] == "ask"
        assert len((await client.get("/api/v1/action-policies")).json()) == 1

        deleted = await client.delete(f"/api/v1/action-policies/{row['id']}")
        assert deleted.status_code == 204
        assert (await client.get("/api/v1/action-policies")).json() == []


async def test_effective_policies_for_a_scope(api: Any, api_engine: ActionEngine) -> None:
    async with api.client() as client:
        await client.put(
            "/api/v1/action-policies",
            json={
                "scopeKind": "site",
                "scopeId": ORIGIN,
                "actionClass": "write_soft",
                "mode": "auto",
            },
        )
        response = await client.get(
            "/api/v1/action-policies/effective",
            params={"url": WRITE_URL},
        )
        assert response.status_code == 200, response.text
    modes = {item["actionClass"]: item["mode"] for item in response.json()}
    assert modes == {
        "read": "auto",
        "write_soft": "auto",
        "write_destructive": "ask",
        "money": "ask",
        "auth": "ask",
    }
    soft = next(item for item in response.json() if item["actionClass"] == "write_soft")
    assert (soft["scopeKind"], soft["scopeId"]) == ("site", "fakeuni.test")


async def test_invalid_policy_is_a_validation_error(api: Any, api_engine: ActionEngine) -> None:
    async with api.client() as client:
        response = await client.put(
            "/api/v1/action-policies",
            json={
                "scopeKind": "global",
                "scopeId": "fakeuni.test",
                "actionClass": "write_soft",
                "mode": "auto",
            },
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation"


async def test_journal_decision_and_execution_flow(api: Any, api_engine: ActionEngine) -> None:
    report = await api_engine.run(
        user_id=api.user_id, request=write_request(), run_id=RUN_ID, ask=None
    )
    assert report.status == "approval_required"
    assert deep_writes() == 0
    action_id = report.entry.id

    async with api.client() as client:
        listed = (await client.get("/api/v1/actions")).json()
        assert len(listed) == 1
        assert listed[0]["id"] == action_id
        assert listed[0]["decision"] == "asked"
        assert listed[0]["request"] is None

        one = await client.get(f"/api/v1/actions/{action_id}")
        assert one.status_code == 200
        assert one.json()["proposal"]["method"] == "POST"

        approved = await client.post(
            f"/api/v1/actions/{action_id}/decision", json={"decision": "approve"}
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["decision"] == "approved"
        assert approved.json()["decidedAt"] is not None

        conflict = await client.post(
            f"/api/v1/actions/{action_id}/decision", json={"decision": "deny"}
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "conflict"

    executed = await api_engine.run(
        user_id=api.user_id, request=write_request(), run_id=RUN_ID, ask=None
    )
    assert executed.status == "executed"
    assert deep_writes() == 1

    async with api.client() as client:
        final = (await client.get(f"/api/v1/actions/{action_id}")).json()
    assert final["decision"] == "executed"
    assert final["request"]["body_sha256"]
    assert final["result"]["status"] == 200


async def test_deny_closes_the_action(api: Any, api_engine: ActionEngine) -> None:
    report = await api_engine.run(
        user_id=api.user_id,
        request=write_request(intent="Отправить заявку в деканат"),
        run_id=RUN_ID,
        ask=None,
    )
    async with api.client() as client:
        denied = await client.post(
            f"/api/v1/actions/{report.entry.id}/decision", json={"decision": "deny"}
        )
        assert denied.status_code == 200, denied.text
        assert denied.json()["decision"] == "denied"

    retried = await api_engine.run(
        user_id=api.user_id,
        request=write_request(intent="Отправить заявку в деканат"),
        run_id=RUN_ID,
        ask=None,
    )
    assert retried.status == "denied"
    assert deep_writes() == 0


async def test_another_user_cannot_read_or_decide(api: Any, api_engine: ActionEngine) -> None:
    report = await api_engine.run(
        user_id=api.user_id, request=write_request(), run_id=RUN_ID, ask=None
    )
    other = await add_user(api.sm, "other")
    api.login_as(other)
    async with api.client() as client:
        assert (await client.get("/api/v1/actions")).json() == []
        missing = await client.get(f"/api/v1/actions/{report.entry.id}")
        assert missing.status_code == 404
        decision = await client.post(
            f"/api/v1/actions/{report.entry.id}/decision", json={"decision": "approve"}
        )
        assert decision.status_code == 404


async def test_journal_filters(api: Any, api_engine: ActionEngine) -> None:
    # A paid intent is classified `money` (a different class from the certificate request).
    await api_engine.run(
        user_id=api.user_id,
        request=write_request(url=f"{ORIGIN}/pay-fee", intent="Оплатить взнос за общежитие"),
        run_id=RUN_ID,
        ask=None,
    )
    await api_engine.run(user_id=api.user_id, request=write_request(), run_id=RUN_ID, ask=None)
    async with api.client() as client:
        money = (await client.get("/api/v1/actions", params={"action_class": "money"})).json()
        asked = (await client.get("/api/v1/actions", params={"decision": "asked"})).json()
        limited = (await client.get("/api/v1/actions", params={"limit": 1})).json()
    assert len(money) == 1
    assert len(asked) == 2
    assert len(limited) == 1
