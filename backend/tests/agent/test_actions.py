"""Write-action skeleton: proposal, one-time approval, handoff, execution (security.md §5, C9)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from botai.agent import actions
from botai.agent.actions import (
    PROPOSAL_TTL,
    FormField,
    ParsedForm,
    fields_hash,
    handle_action,
    propose,
    request_hash,
)
from botai.contracts.agent_ui import ApprovalBlock, HandoffBlock, TextBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext, JobKind
from botai.core.queue import InMemoryJobQueue
from botai.core.services import Services
from botai.db.models import ActionProposalRow, AuditLog, Connection
from tests.agent.conftest import Api, Sm, add_user
from tests.agent.fake_form import FakeuniFormExecutor
from tests.agent.seed import add_connection, add_space
from tests.agent.support import NOW

URL = "https://fakeuni.test/forms/transcript"


@pytest.fixture(autouse=True)
def no_executor() -> Iterator[None]:
    actions.set_executor(None)
    yield
    actions.set_executor(None)


def transcript(conn_id: str, **kw: object) -> ParsedForm:
    base = ParsedForm(
        connection_id=conn_id,
        intent="Заказать справку об обучении",
        method="POST",
        url=URL,
        fields=(
            FormField("language", "Язык", "ru", editable=True),
            FormField("copies", "Количество копий", "1", editable=True),
            FormField("student_id", "Номер студента", "S-12345", editable=False),
        ),
        page_url="https://fakeuni.test/portal/transcript",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


@dataclass
class Rig:
    sm: Sm
    user_id: str
    conn_id: str
    queue: InMemoryJobQueue
    services: Services
    received: list[dict[str, str]]

    def ctx(self) -> InMemoryJobContext:
        return InMemoryJobContext(user_id=self.user_id, job_id=new_id(), connection_id=self.conn_id)

    async def propose(self, **kw: object) -> ApprovalBlock:
        block = await propose(self.ctx(), self.services, transcript(self.conn_id, **kw))
        assert isinstance(block, ApprovalBlock)
        return block

    def body(self, block: ApprovalBlock, **edits: str) -> dict[str, object]:
        shown = {f.name: f.value for f in block.proposal.request.fields}
        return {"fields": {**shown, **edits}, "fieldsHash": fields_hash(shown)}


@pytest.fixture
async def rig(api: Api, user_id: str) -> Rig:
    space = await add_space(api.sm, user_id)
    conn = await add_connection(api.sm, user_id, space, "Личный кабинет", kind="portal")
    received: list[dict[str, str]] = []
    actions.set_executor(FakeuniFormExecutor(received))
    return Rig(api.sm, user_id, conn, api.queue, api.services, received)


async def row_of(sm: Sm, proposal_id: str) -> ActionProposalRow:
    async with sm() as session:
        return (
            await session.execute(
                select(ActionProposalRow).where(ActionProposalRow.id == proposal_id)
            )
        ).scalar_one()


async def audit_kinds(sm: Sm, user_id: str) -> list[str]:
    async with sm() as session:
        rows = await session.execute(
            select(AuditLog.kind).where(AuditLog.user_id == user_id).order_by(AuditLog.id)
        )
        return list(rows.scalars())


async def approve(api: Api, proposal_id: str, body: dict[str, object]) -> int:
    async with api.client() as client:
        return (await client.post(f"/api/v1/actions/{proposal_id}/approve", json=body)).status_code


# --- proposal -----------------------------------------------------------------------------------


async def test_a_proposal_shows_exactly_what_will_be_sent(rig: Rig) -> None:
    ctx = rig.ctx()
    block = await propose(ctx, rig.services, transcript(rig.conn_id))
    assert isinstance(block, ApprovalBlock)
    assert ctx.blocks == [block]
    request = block.proposal.request
    assert (request.method, str(request.url), request.body_kind) == ("POST", URL, "form")
    assert [(f.name, f.value, f.editable) for f in request.fields] == [
        ("language", "ru", True),
        ("copies", "1", True),
        ("student_id", "S-12345", False),
    ]
    assert block.proposal.expires_at == NOW + PROPOSAL_TTL
    row = await row_of(rig.sm, block.proposal.id)
    assert (row.state, row.user_id, row.connection_id) == ("pending", rig.user_id, rig.conn_id)
    assert row.fields_hash == fields_hash(
        {"language": "ru", "copies": "1", "student_id": "S-12345"}
    )


async def test_a_form_that_cannot_be_sent_by_the_server_becomes_a_handoff(rig: Rig) -> None:
    ctx = rig.ctx()
    block = await propose(ctx, rig.services, transcript(rig.conn_id, server_submit=False))
    assert isinstance(block, HandoffBlock)
    assert str(block.url) == "https://fakeuni.test/portal/transcript"
    assert "Заказать справку об обучении" in block.text
    assert "Язык: ru" in block.text
    assert "Номер студента: S-12345" in block.text
    async with rig.sm() as session:
        assert (await session.execute(select(ActionProposalRow))).scalars().all() == []


async def test_without_an_enabled_executor_everything_is_a_handoff(rig: Rig) -> None:
    actions.set_executor(None)  # production default: real sending is not enabled
    block = await propose(rig.ctx(), rig.services, transcript(rig.conn_id))
    assert isinstance(block, HandoffBlock)


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"url": "https://evil.test/forms/transcript"}, ErrorCode.EGRESS_BLOCKED),
        ({"url": "http://fakeuni.test/forms/transcript"}, ErrorCode.EGRESS_BLOCKED),
        ({"url": "https://fakeuni.test/account/delete"}, ErrorCode.FORBIDDEN),
        ({"url": "https://fakeuni.test/profile/password"}, ErrorCode.FORBIDDEN),
        ({"intent": "Отозвать работу (delete submission)"}, ErrorCode.FORBIDDEN),
        ({"method": "DELETE"}, ErrorCode.VALIDATION),
        ({"method": "GET"}, ErrorCode.VALIDATION),
    ],
)
async def test_only_allowed_requests_can_be_proposed(
    rig: Rig, kw: dict[str, str], code: ErrorCode
) -> None:
    with pytest.raises(BotaiError) as exc:
        await propose(rig.ctx(), rig.services, transcript(rig.conn_id, **kw))
    assert exc.value.code == code


async def test_a_foreign_connection_cannot_be_used(rig: Rig) -> None:
    other = await add_user(rig.sm, "other")
    space = await add_space(rig.sm, other)
    theirs = await add_connection(rig.sm, other, space, "Чужое")
    with pytest.raises(BotaiError) as exc:
        await propose(rig.ctx(), rig.services, transcript(theirs))
    assert exc.value.code == ErrorCode.NOT_FOUND


# --- approval -----------------------------------------------------------------------------------


async def test_approval_is_recorded_and_queues_one_action_job(api: Api, rig: Rig) -> None:
    block = await rig.propose()
    status = await approve(api, block.proposal.id, rig.body(block, copies="2"))
    assert status == 202

    row = await row_of(rig.sm, block.proposal.id)
    assert row.state == "approved"
    assert row.approved_at == NOW
    assert {f["name"]: f["value"] for f in row.proposal["request"]["fields"]}["copies"] == "2"
    final = {"language": "ru", "copies": "2", "student_id": "S-12345"}
    assert (row.result or {})["approvedHash"] == request_hash("POST", URL, final)
    assert (row.result or {})["edited"] == ["copies"]

    (job,) = api.queue.jobs
    assert job["kind"] == JobKind.ACTION
    assert job["payload"] == {"proposal_id": block.proposal.id}
    assert job["connection_id"] == rig.conn_id
    assert await audit_kinds(rig.sm, rig.user_id) == ["action_approved"]
    async with rig.sm() as session:
        detail = (await session.execute(select(AuditLog.detail))).scalar_one()
    assert detail["edited_fields"] == ["copies"]
    assert "S-12345" not in str(detail)  # names and hashes only, never field values


async def test_an_approval_works_once(api: Api, rig: Rig) -> None:
    block = await rig.propose()
    assert await approve(api, block.proposal.id, rig.body(block)) == 202
    assert await approve(api, block.proposal.id, rig.body(block)) == 409
    assert len(api.queue.jobs) == 1


async def test_content_the_student_did_not_see_is_refused(api: Api, rig: Rig) -> None:
    block = await rig.propose()
    stale_view = rig.body(block)
    stale_view["fieldsHash"] = fields_hash({"language": "en"})
    async with api.client() as client:
        response = await client.post(
            f"/api/v1/actions/{block.proposal.id}/approve", json=stale_view
        )
    assert response.status_code == 409
    assert response.json()["code"] == "action_changed"
    assert api.queue.jobs == []
    assert (await row_of(rig.sm, block.proposal.id)).state == "pending"


async def test_only_editable_fields_may_differ(api: Api, rig: Rig) -> None:
    block = await rig.propose()
    assert await approve(api, block.proposal.id, rig.body(block, student_id="S-99999")) == 409
    assert await approve(api, block.proposal.id, rig.body(block, extra="x")) == 409
    missing = rig.body(block)
    del missing["fields"]["language"]
    assert await approve(api, block.proposal.id, missing) == 409
    assert api.queue.jobs == []
    assert (await row_of(rig.sm, block.proposal.id)).state == "pending"


async def test_an_expired_proposal_cannot_be_approved(api: Api, rig: Rig) -> None:
    block = await rig.propose()
    rig.services.clock.advance(PROPOSAL_TTL + timedelta(seconds=1))
    async with api.client() as client:
        response = await client.post(
            f"/api/v1/actions/{block.proposal.id}/approve", json=rig.body(block)
        )
    assert response.status_code == 410
    assert response.json()["code"] == "action_expired"
    assert api.queue.jobs == []
    assert (await row_of(rig.sm, block.proposal.id)).state == "expired"


async def test_a_rejected_proposal_stays_rejected(api: Api, rig: Rig) -> None:
    block = await rig.propose()
    async with api.client() as client:
        assert (await client.post(f"/api/v1/actions/{block.proposal.id}/reject")).status_code == 204
        second = await client.post(f"/api/v1/actions/{block.proposal.id}/reject")
    assert second.status_code == 409
    assert await approve(api, block.proposal.id, rig.body(block)) == 409
    assert api.queue.jobs == []
    assert await audit_kinds(rig.sm, rig.user_id) == ["action_rejected"]


async def test_another_student_cannot_touch_a_proposal(api: Api, rig: Rig) -> None:
    block = await rig.propose()
    other = await add_user(rig.sm, "other")
    api.login_as(other)
    assert await approve(api, block.proposal.id, rig.body(block)) == 404
    async with api.client() as client:
        assert (await client.post(f"/api/v1/actions/{block.proposal.id}/reject")).status_code == 404
    assert (await row_of(rig.sm, block.proposal.id)).state == "pending"


# --- execution ----------------------------------------------------------------------------------


async def approved(api: Api, rig: Rig, **edits: str) -> ApprovalBlock:
    block = await rig.propose()
    assert await approve(api, block.proposal.id, rig.body(block, **edits)) == 202
    return block


async def test_the_approved_request_is_sent_exactly_once_with_the_approved_fields(
    api: Api, rig: Rig
) -> None:
    block = await approved(api, rig, copies="2")
    ctx = rig.ctx()
    payload = api.queue.jobs[0]["payload"]
    out = await handle_action(ctx, rig.services, payload)

    assert rig.received == [{"language": "ru", "copies": "2", "student_id": "S-12345"}]
    assert out == {"state": "executed", "status": 200}
    assert [b.markdown for b in ctx.blocks if isinstance(b, TextBlock)] == ["Заявка отправлена."]
    row = await row_of(rig.sm, block.proposal.id)
    assert (row.state, row.executed_at) == ("executed", NOW)
    assert await audit_kinds(rig.sm, rig.user_id) == ["action_approved", "action_executed"]

    with pytest.raises(BotaiError) as exc:  # a retried job must not send again
        await handle_action(rig.ctx(), rig.services, payload)
    assert exc.value.code == ErrorCode.CONFLICT
    assert len(rig.received) == 1


async def test_a_request_changed_after_approval_is_not_sent(api: Api, rig: Rig) -> None:
    block = await approved(api, rig)
    async with rig.sm() as session:
        row = (
            await session.execute(
                select(ActionProposalRow).where(ActionProposalRow.id == block.proposal.id)
            )
        ).scalar_one()
        tampered = dict(row.proposal)
        tampered["request"] = {**tampered["request"], "url": "https://fakeuni.test/forms/other"}
        row.proposal = tampered
        await session.commit()
    with pytest.raises(BotaiError) as exc:
        await handle_action(rig.ctx(), rig.services, api.queue.jobs[0]["payload"])
    assert exc.value.code == ErrorCode.ACTION_CHANGED
    assert rig.received == []
    assert (await row_of(rig.sm, block.proposal.id)).state == "failed"


async def test_the_deny_list_wins_even_over_a_valid_approval(api: Api, rig: Rig) -> None:
    block = await approved(api, rig)
    forbidden = "https://fakeuni.test/account/delete"
    async with rig.sm() as session:
        row = (
            await session.execute(
                select(ActionProposalRow).where(ActionProposalRow.id == block.proposal.id)
            )
        ).scalar_one()
        proposal = dict(row.proposal)
        proposal["request"] = {**proposal["request"], "url": forbidden}
        final = {f["name"]: f["value"] for f in proposal["request"]["fields"]}
        row.proposal = proposal
        row.result = {**(row.result or {}), "approvedHash": request_hash("POST", forbidden, final)}
        await session.commit()
    with pytest.raises(BotaiError) as exc:
        await handle_action(rig.ctx(), rig.services, api.queue.jobs[0]["payload"])
    assert exc.value.code == ErrorCode.FORBIDDEN
    assert rig.received == []


async def test_the_connection_allowlist_applies_at_execution(api: Api, rig: Rig) -> None:
    block = await approved(api, rig)
    async with rig.sm() as session:
        conn = await session.get(Connection, rig.conn_id)
        assert conn is not None
        conn.allow_hosts = ["other.test"]  # the connection no longer allows this host
        await session.commit()
    with pytest.raises(BotaiError) as exc:
        await handle_action(rig.ctx(), rig.services, api.queue.jobs[0]["payload"])
    assert exc.value.code == ErrorCode.EGRESS_BLOCKED
    assert rig.received == []
    assert (await row_of(rig.sm, block.proposal.id)).state == "failed"


async def test_a_site_error_is_reported_and_not_retried(api: Api, rig: Rig) -> None:
    block = await rig.propose(url="https://fakeuni.test/forms/broken")
    assert await approve(api, block.proposal.id, rig.body(block)) == 202
    ctx = rig.ctx()
    out = await handle_action(ctx, rig.services, api.queue.jobs[0]["payload"])
    assert out == {"state": "failed", "status": 500}
    assert len(rig.received) == 1
    assert any("не принял" in b.markdown for b in ctx.blocks if isinstance(b, TextBlock))
    assert "action_failed" in await audit_kinds(rig.sm, rig.user_id)


async def test_with_sending_disabled_an_approved_action_ends_as_a_handoff(
    api: Api, rig: Rig
) -> None:
    block = await approved(api, rig, copies="3")
    actions.set_executor(None)  # sending was switched off between approval and execution
    ctx = rig.ctx()
    out = await handle_action(ctx, rig.services, api.queue.jobs[0]["payload"])
    assert out == {"state": "handoff"}
    (hand,) = [b for b in ctx.blocks if isinstance(b, HandoffBlock)]
    assert "Количество копий: 3" in hand.text
    assert rig.received == []
    assert (await row_of(rig.sm, block.proposal.id)).state == "handoff"


def test_the_hash_is_stable_and_order_independent() -> None:
    assert fields_hash({"a": "1", "b": "2"}) == fields_hash({"b": "2", "a": "1"})
    assert fields_hash({"a": "1"}) != fields_hash({"a": "2"})
    assert request_hash("POST", URL, {"a": "1"}) != request_hash("PUT", URL, {"a": "1"})
    assert request_hash("POST", URL, {"a": "1"}) != request_hash("POST", URL + "x", {"a": "1"})
