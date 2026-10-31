"""The v3 ACTION job through the v4 action action_engine: approved hash, journal, never policy.

The v3 proposal path is exercised end-to-end (propose → one-time approval → queued ACTION job)
and only the execution is routed through the v4 policy engine by `agent_engine=v4`. The v3 path
itself is covered by `tests/agent/test_actions.py` and is not changed here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select

from botai.agent import actions as v3_actions
from botai.agent.actions import FormField, ParsedForm, fields_hash
from botai.agent.schemas import ApproveIn
from botai.contracts.agent_ui import ApprovalBlock, TextBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import InMemoryJobContext
from botai.db.models import ActionLog, ActionProposalRow, AuditLog
from tests.actions.conftest import ORIGIN, WRITE_URL, deep_writes
from tests.actions.support import allow
from tests.agent.fake_form import FakeuniFormExecutor
from tests.agent.seed import add_connection, add_space

pytestmark = pytest.mark.db


@dataclass(slots=True)
class Rig:
    api: Any
    conn_id: str
    action_engine: Any

    def ctx(self) -> InMemoryJobContext:
        return InMemoryJobContext(
            user_id=self.api.user_id,
            job_id="01M68Z000000000000000000J1",
            connection_id=self.conn_id,
        )

    def form(self, *, intent: str = "Оформить справку об обучении") -> ParsedForm:
        return ParsedForm(
            connection_id=self.conn_id,
            intent=intent,
            method="POST",
            url=WRITE_URL,
            fields=(
                FormField("language", "Язык", "ru", editable=True),
                FormField("student_id", "Номер студента", "S-12345", editable=False),
            ),
            page_url=f"{ORIGIN}/portal/transcript",
        )


@pytest.fixture
async def rig(api: Any, action_engine: Any) -> AsyncIterator[Rig]:
    user = api.user_id
    space = await add_space(api.sm, user)
    conn = await add_connection(api.sm, user, space, "Личный кабинет", kind="portal")
    v3_actions.set_executor(FakeuniFormExecutor([]))  # propose keeps the server-submit path
    try:
        yield Rig(api=api, conn_id=conn, action_engine=action_engine)
    finally:
        v3_actions.set_executor(None)


async def approved_proposal(rig: Rig) -> tuple[str, dict[str, Any]]:
    """A v3 proposal approved through the v3 API path: (proposal_id, ACTION job payload)."""
    api = rig.api
    block = await v3_actions.propose(rig.ctx(), api.services, rig.form())
    assert isinstance(block, ApprovalBlock)
    shown = {field.name: field.value for field in block.proposal.request.fields}
    body = ApproveIn(fields=shown, fields_hash=fields_hash(shown))
    async with api.sm() as session:
        await v3_actions.approve(session, api.services, api.user_id, block.proposal.id, body)
    (job,) = api.queue.jobs
    assert job["kind"] == "action"
    return block.proposal.id, job["payload"]


async def test_v4_bridge_executes_the_approved_request_and_journals_it(rig: Rig) -> None:
    api = rig.api
    proposal_id, payload = await approved_proposal(rig)
    api.services.settings.agent_engine = "v4"
    ctx = rig.ctx()

    out = await v3_actions.handle_action(ctx, api.services, payload, engine=rig.action_engine)

    assert out == {"state": "executed", "status": 200}
    assert deep_writes() == 1
    assert [block.markdown for block in ctx.blocks if isinstance(block, TextBlock)] == [
        "Заявка отправлена."
    ]

    journal = await rig.action_engine.history(user_id=api.user_id)
    assert len(journal) == 1
    row = journal[0]
    assert row.decision == "executed"
    assert row.idempotency_key == f"legacy:{proposal_id}"
    # InMemoryJobContext has no `jobs` row, so the journal keeps no dangling FK.
    assert row.job_id is None
    assert row.request is not None
    assert (row.request["method"], row.request["url"]) == ("POST", WRITE_URL)
    assert row.request["body_sha256"] is not None

    async with api.sm() as session:
        proposal_row = (
            await session.execute(
                select(ActionProposalRow).where(ActionProposalRow.id == proposal_id)
            )
        ).scalar_one()
        assert proposal_row.state == "executed"
        assert proposal_row.executed_at is not None
        kinds = list(
            (
                await session.execute(
                    select(AuditLog.kind)
                    .where(AuditLog.user_id == api.user_id)
                    .order_by(AuditLog.id)
                )
            ).scalars()
        )
    assert kinds == ["action_approved", "action_executed"]

    # A retried ACTION job cannot send twice: the v3 claim is already taken.
    with pytest.raises(BotaiError) as exc:
        await v3_actions.handle_action(rig.ctx(), api.services, payload, engine=rig.action_engine)
    assert exc.value.code == ErrorCode.CONFLICT
    assert deep_writes() == 1


async def test_v4_bridge_obeys_a_never_policy(rig: Rig) -> None:
    api = rig.api
    await allow(rig.action_engine, api.user_id, mode="never")
    proposal_id, payload = await approved_proposal(rig)
    api.services.settings.agent_engine = "v4"

    with pytest.raises(BotaiError) as exc:
        await v3_actions.handle_action(rig.ctx(), api.services, payload, engine=rig.action_engine)

    assert exc.value.code == ErrorCode.FORBIDDEN
    assert deep_writes() == 0
    journal = await rig.action_engine.history(user_id=api.user_id)
    assert [row.decision for row in journal] == ["never"]
    async with api.sm() as session:
        proposal_row = (
            await session.execute(
                select(ActionProposalRow).where(ActionProposalRow.id == proposal_id)
            )
        ).scalar_one()
        assert proposal_row.state == "failed"
        stored = list((await session.execute(select(ActionLog))).scalars())
    assert len(stored) == 1  # the journal has no body values, only the proposal/decision
