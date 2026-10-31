"""The v3 `ACTION` job on the v4 engine: an approved v3 proposal becomes a journaled v4 action.

The v3 path is kept as-is behind `agent_engine=v3` (`agent/actions.py`). With
`BOTAI_AGENT_ENGINE=v4` an approved proposal goes through this bridge instead: the approved hash
is re-verified exactly as in v3, and the concrete request is then handed to the v4 `ActionEngine`
— owner policy (`never` refuses, `ask` is already answered by the v3 `ApprovalBlock`, `auto`
runs), one idempotency key per proposal, one `actions_log` row, one send. The v3 proposal row
still transitions to `executed`/`failed` and the same `TextBlock` is emitted, so the older UI
and the job result shape keep working.

What changes on purpose (D-006): the v4 path does not re-apply the v3 `DENY_LIST` or the
connection host allowlist. Those were the v3 architecture; the v4 policy is the owner's
`auto/ask/never` policy, and the request being executed is exactly the one the owner approved.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.actions.engine import ActionEngine
from botai.actions.models import ActionRequest
from botai.agent.actions import (
    APPROVED,
    EXECUTED,
    FAILED,
    request_hash,
)
from botai.agent.actions import (
    ActionExecutor as LegacyExecutor,
)
from botai.contracts.agent_ui import ActionProposal, TextBlock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.jobs import JobContext
from botai.core.services import Services
from botai.db.models import ActionProposalRow, AuditLog, Job

_BODY_KINDS: dict[str, Literal["json", "form", "raw"]] = {
    "form": "form",
    "multipart": "form",
    "json": "json",
}


async def handle_approved_action(
    ctx: JobContext,
    services: Services,
    payload: dict[str, Any],
    *,
    engine: ActionEngine | None = None,
    executor: LegacyExecutor | None = None,
) -> dict[str, Any] | None:
    """Execute exactly one approved v3 proposal through the v4 policy engine and journal."""
    proposal_id = str(payload["proposal_id"])
    now = services.clock.now()
    action_engine = engine or ActionEngine(db=services.db, clock=services.clock.now)
    if executor is not None:
        # A v3 executor is a different protocol (it takes JobContext). The v4 engine sends HTTP
        # through ActionExecutor; the v3 executor injection therefore does not apply here.
        raise BotaiError(
            ErrorCode.NOT_IMPLEMENTED,
            "v3-исполнитель действий не используется в v4: отправка идёт через журнал действий",
        )
    proposal, job_link = await _claim_proposal(ctx, services, proposal_id, now=now)

    action_request = ActionRequest(
        transport="http",
        intent=proposal.intent,
        method=proposal.request.method,
        url=str(proposal.request.url),
        body={field.name: field.value for field in proposal.request.fields},
        body_kind=_BODY_KINDS.get(proposal.request.body_kind, "json"),
        idempotency_key=f"legacy:{proposal_id}",
    )
    try:
        prepared = await action_engine.prepare(
            user_id=ctx.user_id, request=action_request, job_id=job_link
        )
        if prepared.policy.mode == "never":
            raise BotaiError(
                ErrorCode.FORBIDDEN,
                "Политика владельца запрещает этот класс действий: заявка не отправлена",
            )
        if prepared.entry.decision == "asked":
            decided = await action_engine.decide(
                user_id=ctx.user_id, action_id=prepared.entry.id, approve=True
            )
            prepared = replace(prepared, entry=decided)
        report = await action_engine.execute_prepared(prepared, user_id=ctx.user_id)
    except BotaiError as exc:
        await _finish_v3(
            services,
            user_id=ctx.user_id,
            proposal_id=proposal_id,
            state=FAILED,
            message_extra={},
            reason=exc.code.value,
        )
        raise
    except Exception:
        await _finish_v3(
            services,
            user_id=ctx.user_id,
            proposal_id=proposal_id,
            state=FAILED,
            message_extra={},
            reason="internal_error",
        )
        raise

    outcome = report.outcome
    ok = report.status == "executed"
    status = outcome.status if outcome is not None else None
    if ok:
        message = "Заявка отправлена."
    elif outcome is not None and outcome.detail:
        message = outcome.detail
    else:
        message = "Сайт вуза не принял заявку."
    await _finish_v3(
        services,
        user_id=ctx.user_id,
        proposal_id=proposal_id,
        state=EXECUTED if ok else FAILED,
        message_extra={"status": status, "journal_action_id": report.entry.id},
        reason=None,
    )
    await ctx.emit(TextBlock(id=new_id(), at=now, markdown=message[:8000]))
    return {"state": EXECUTED if ok else FAILED, "status": status}


async def _claim_proposal(
    ctx: JobContext, services: Services, proposal_id: str, *, now: Any
) -> tuple[ActionProposal, str | None]:
    """Load and validate the approved proposal; mark it executed before any send (v3 claim)."""
    async with services.db() as session:
        row = (
            await session.execute(
                select(ActionProposalRow).where(
                    ActionProposalRow.id == proposal_id,
                    ActionProposalRow.user_id == ctx.user_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Предложение не найдено")
        if row.state != APPROVED or row.executed_at is not None:
            raise BotaiError(ErrorCode.CONFLICT, "Это действие уже выполнено или не подтверждено")
        proposal = ActionProposal.model_validate(row.proposal)
        request = proposal.request
        final = {field.name: field.value for field in request.fields}
        approved_hash = (row.result or {}).get("approvedHash")
        if approved_hash != request_hash(request.method, str(request.url), final):
            await _close_unexecuted(session, row, ctx.user_id, reason="action_changed")
            raise BotaiError(ErrorCode.ACTION_CHANGED, "Запрос изменился после подтверждения")
        row.executed_at = now  # claimed before sending: a retried job cannot send twice
        # A job context can exist without a database job row (tests, one-off runs); the journal's
        # `job_id` is provenance with an FK, so link it only when the row is real.
        job_link = await session.scalar(select(Job.id).where(Job.id == ctx.job_id))
        await session.commit()
        return proposal, job_link


async def _close_unexecuted(
    session: AsyncSession, row: ActionProposalRow, user_id: str, *, reason: str
) -> None:
    row.state = FAILED
    session.add(
        AuditLog(
            user_id=user_id,
            kind="action_failed",
            detail={
                "proposal_id": row.id,
                "connection_id": row.connection_id,
                "reason": reason,
            },
        )
    )
    await session.commit()


async def _finish_v3(
    services: Services,
    *,
    user_id: str,
    proposal_id: str,
    state: str,
    message_extra: dict[str, Any],
    reason: str | None,
) -> None:
    """Close the v3 proposal row and write the v3 audit entry for the v4 execution."""
    async with services.db() as session:
        row = (
            await session.execute(
                select(ActionProposalRow).where(
                    ActionProposalRow.id == proposal_id,
                    ActionProposalRow.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:  # pragma: no cover - the row cannot disappear mid-job
            return
        row.state = state
        if state == EXECUTED:
            row.result = {**(row.result or {}), **message_extra}
        session.add(
            AuditLog(
                user_id=user_id,
                kind="action_executed" if state == EXECUTED else "action_failed",
                detail={
                    "proposal_id": proposal_id,
                    "connection_id": row.connection_id,
                    **message_extra,
                    **({"reason": reason} if reason else {}),
                },
            )
        )
        await session.commit()


__all__ = ["handle_approved_action"]
