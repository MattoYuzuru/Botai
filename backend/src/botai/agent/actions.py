"""Write actions, skeleton (priority 3, Q2; security.md §5, T14).

BotAI is read-only by default. A write action goes through four steps:

1. **Proposal.** From a parsed form the agent builds one concrete request (method, URL, every
   field). If the form cannot be sent through the server (old form, extra protection), or sending
   is not enabled in this deployment, the student gets a `HandoffBlock` instead: the filled text
   and a link, and sends it themselves.
2. **Showing.** The `ApprovalBlock` carries exactly what will go out. Text that came from a page
   is data in the form fields and cannot change the request.
3. **Approval.** One-time (`pending -> approved` is a single atomic update), valid for
   `PROPOSAL_TTL`, bound to the fields the student saw (`action_changed` otherwise) and to the
   exact method and URL stored on the server. Only fields marked editable may differ.
4. **Execution.** The ACTION job re-verifies the stored request against the approved hash and the
   connection's host allowlist, writes the audit record, then calls the registered executor.

No executor is registered in production: real sending is not enabled. Tests register one that
posts to the synthetic university.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.schemas import ApproveIn
from botai.contracts.agent_ui import (
    ActionProposal,
    AgentUiBlock,
    ApprovalBlock,
    HandoffBlock,
    ProposalField,
    ProposalRequest,
    TextBlock,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.core.http import host_allowed
from botai.core.ids import new_id
from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services
from botai.db.models import ActionProposalRow, AuditLog, Connection

log = logging.getLogger("botai.agent.actions")

PROPOSAL_TTL = timedelta(minutes=5)
METHODS = ("POST", "PUT", "PATCH")
# security.md §5: irreversible actions are not executed at all, even with an approval.
DENY_LIST = re.compile(
    r"logout|log-out|sign-?out|delete|remove|revoke|unsubscribe|password|passwd|payment|"
    r"\bpay\b|late-days|final-?submit|withdraw",
    re.IGNORECASE,
)

PENDING, APPROVED, REJECTED, EXPIRED = "pending", "approved", "rejected", "expired"
EXECUTED, FAILED, HANDED_OFF = "executed", "failed", "handoff"


@dataclass(frozen=True, slots=True)
class FormField:
    name: str
    label: str
    value: str
    editable: bool = True


@dataclass(frozen=True, slots=True)
class ParsedForm:
    """A form the agent has read and understood. Built from the page's HTML, not from model text."""

    connection_id: str
    intent: str
    method: str
    url: str
    fields: tuple[FormField, ...]
    body_kind: Literal["form", "json", "multipart"] = "form"
    reversible: bool = False
    server_submit: bool = True  # False: the student has to send it (old form, extra protection)
    page_url: str | None = None  # where the student sends it by hand (handoff link)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    ok: bool
    status: int | None
    message: str


class ActionExecutor(Protocol):
    async def execute(
        self,
        ctx: JobContext,
        *,
        allow_hosts: Sequence[str],
        method: str,
        url: str,
        body_kind: str,
        fields: Mapping[str, str],
    ) -> ExecutionResult: ...


_executor: ActionExecutor | None = None


def set_executor(executor: ActionExecutor | None) -> None:
    """Enable (tests) or disable real sending. Production leaves this unset."""
    global _executor
    _executor = executor


def get_executor() -> ActionExecutor | None:
    return _executor


# --- hashing ------------------------------------------------------------------------------------


def canonical_fields(fields: Mapping[str, str]) -> str:
    return json.dumps(dict(sorted(fields.items())), ensure_ascii=False, separators=(",", ":"))


def fields_hash(fields: Mapping[str, str]) -> str:
    """sha256 of the canonical fields: what the student saw / what was approved."""
    return hashlib.sha256(canonical_fields(fields).encode()).hexdigest()


def request_hash(method: str, url: str, fields: Mapping[str, str]) -> str:
    """Binds an approval to the exact method, URL and body."""
    payload = f"{method}\n{url}\n{canonical_fields(fields)}"
    return hashlib.sha256(payload.encode()).hexdigest()


# --- 1-2. proposal ------------------------------------------------------------------------------


def check_request(allow_hosts: Sequence[str], method: str, url: str, intent: str) -> None:
    """Gate shared by proposing and executing. Raises BotaiError on anything not allowed."""
    if method not in METHODS:
        raise BotaiError(ErrorCode.VALIDATION, "Такие запросы агент не отправляет")
    parts = urlsplit(url)
    if DENY_LIST.search(parts.path) or DENY_LIST.search(parts.query) or DENY_LIST.search(intent):
        raise BotaiError(
            ErrorCode.FORBIDDEN, "Это действие агент не выполняет ни при каких условиях"
        )
    if not host_allowed(url, allow_hosts):
        raise BotaiError(
            ErrorCode.EGRESS_BLOCKED,
            f"Адрес {parts.hostname} не входит в разрешённые для этого подключения",
            extra={"host": parts.hostname},
        )


def handoff_text(form: ParsedForm) -> str:
    lines = [form.intent, ""]
    lines += [f"{f.label}: {f.value}" for f in form.fields]
    return "\n".join(lines)[:8000]


async def _connection(session: AsyncSession, user_id: str, connection_id: str) -> Connection:
    conn = (
        await session.execute(
            select(Connection).where(Connection.id == connection_id, Connection.user_id == user_id)
        )
    ).scalar_one_or_none()
    if conn is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
    return conn


async def propose(ctx: JobContext, services: Services, form: ParsedForm) -> AgentUiBlock:
    """Emit an `ApprovalBlock` (or a `HandoffBlock`) for the parsed form and return it."""
    now = services.clock.now()
    async with services.db() as session:
        conn = await _connection(session, ctx.user_id, form.connection_id)
        allow_hosts = list(conn.allow_hosts)
        check_request(allow_hosts, form.method, form.url, form.intent)

        if not form.server_submit or get_executor() is None:
            block: AgentUiBlock = HandoffBlock(
                id=new_id(),
                at=now,
                title=form.intent[:200],
                url=form.page_url or form.url,  # type: ignore[arg-type]
                text=handoff_text(form),
            )
            await ctx.emit(block)
            return block

        proposal = ActionProposal(
            id=new_id(),
            connection_id=conn.id,
            intent=form.intent[:300],
            reversible=form.reversible,
            expires_at=now + PROPOSAL_TTL,
            request=ProposalRequest(
                method=form.method,  # type: ignore[arg-type]
                url=form.url,  # type: ignore[arg-type]
                body_kind=form.body_kind,
                fields=[
                    ProposalField(name=f.name, label=f.label, value=f.value, editable=f.editable)
                    for f in form.fields
                ],
            ),
        )
        shown = {f.name: f.value for f in form.fields}
        session.add(
            ActionProposalRow(
                id=proposal.id,
                user_id=ctx.user_id,
                connection_id=conn.id,
                proposal=proposal.model_dump(by_alias=True, mode="json"),
                fields_hash=fields_hash(shown),
                state=PENDING,
                expires_at=proposal.expires_at,
            )
        )
        await session.commit()
    block = ApprovalBlock(id=new_id(), at=now, proposal=proposal)
    await ctx.emit(block)
    return block


# --- 3. approval --------------------------------------------------------------------------------


async def _audit(session: AsyncSession, user_id: str, kind: str, detail: dict[str, Any]) -> None:
    session.add(AuditLog(user_id=user_id, kind=kind, detail=detail))


async def _owned(session: AsyncSession, user_id: str, proposal_id: str) -> ActionProposalRow:
    row = (
        await session.execute(
            select(ActionProposalRow).where(
                ActionProposalRow.id == proposal_id, ActionProposalRow.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Предложение не найдено")
    return row


async def approve(
    session: AsyncSession, services: Services, user_id: str, proposal_id: str, body: ApproveIn
) -> str:
    """Approve once. Returns the id of the ACTION job that will execute it."""
    now = services.clock.now()
    row = await _owned(session, user_id, proposal_id)
    proposal = ActionProposal.model_validate(row.proposal)
    if row.state != PENDING:
        raise BotaiError(ErrorCode.CONFLICT, "Это предложение уже обработано")
    if now >= row.expires_at:
        row.state = EXPIRED
        await session.commit()
        raise BotaiError(ErrorCode.ACTION_EXPIRED, "Время подтверждения истекло, запросите заново")

    original = {f.name: f.value for f in proposal.request.fields}
    if body.fields_hash != row.fields_hash:
        raise BotaiError(ErrorCode.ACTION_CHANGED, "Содержимое изменилось, посмотрите его ещё раз")
    editable = {f.name for f in proposal.request.fields if f.editable}
    final = dict(body.fields)
    if final.keys() != original.keys() or any(
        final[name] != original[name] for name in original if name not in editable
    ):
        raise BotaiError(ErrorCode.ACTION_CHANGED, "Поля не совпадают с показанными вам")

    approved_hash = request_hash(proposal.request.method, str(proposal.request.url), final)
    edited = sorted(n for n in editable if final[n] != original[n])
    # The single atomic step: only one caller can move pending -> approved.
    moved = await session.execute(
        update(ActionProposalRow)
        .where(
            ActionProposalRow.id == proposal_id,
            ActionProposalRow.user_id == user_id,
            ActionProposalRow.state == PENDING,
        )
        .values(
            state=APPROVED,
            approved_at=now,
            proposal={
                **row.proposal,
                "request": {
                    **row.proposal["request"],
                    "fields": [
                        {**f, "value": final[f["name"]]} for f in row.proposal["request"]["fields"]
                    ],
                },
            },
            result={"approvedHash": approved_hash, "edited": edited},
        )
    )
    if moved.rowcount != 1:  # type: ignore[attr-defined]
        raise BotaiError(ErrorCode.CONFLICT, "Это предложение уже подтверждено")
    await _audit(
        session,
        user_id,
        "action_approved",
        {
            "proposal_id": proposal_id,
            "connection_id": row.connection_id,
            "method": proposal.request.method,
            "url": str(proposal.request.url),
            "approved_hash": approved_hash,
            "edited_fields": edited,  # names only: values may be personal
        },
    )
    await session.commit()
    return await services.queue.enqueue(
        JobKind.ACTION,
        user_id=user_id,
        connection_id=row.connection_id,
        payload={"proposal_id": proposal_id},
        dedupe=False,  # one job per approval; never merged into another proposal's job
    )


async def reject(session: AsyncSession, user_id: str, proposal_id: str) -> None:
    row = await _owned(session, user_id, proposal_id)
    if row.state != PENDING:
        raise BotaiError(ErrorCode.CONFLICT, "Это предложение уже обработано")
    row.state = REJECTED
    await _audit(session, user_id, "action_rejected", {"proposal_id": proposal_id})
    await session.commit()


# --- 4. execution -------------------------------------------------------------------------------


async def _refuse(
    session: AsyncSession, row: ActionProposalRow, user_id: str, error: BotaiError
) -> None:
    """An approved action that no longer checks out is closed, audited and never sent."""
    row.state = FAILED
    await _audit(
        session,
        user_id,
        "action_failed",
        {"proposal_id": row.id, "connection_id": row.connection_id, "reason": error.code.value},
    )
    await session.commit()
    raise error


async def handle_action(
    ctx: JobContext,
    services: Services,
    payload: dict[str, Any],
    *,
    executor: ActionExecutor | None = None,
) -> dict[str, Any] | None:
    """The ACTION job: execute exactly what was approved, once."""
    proposal_id = str(payload["proposal_id"])
    now = services.clock.now()
    async with services.db() as session:
        row = await _owned(session, ctx.user_id, proposal_id)
        if row.state != APPROVED or row.executed_at is not None:
            raise BotaiError(ErrorCode.CONFLICT, "Это действие уже выполнено или не подтверждено")
        conn = await _connection(session, ctx.user_id, row.connection_id)
        proposal = ActionProposal.model_validate(row.proposal)
        request = proposal.request
        final = {f.name: f.value for f in request.fields}
        approved_hash = (row.result or {}).get("approvedHash")
        if approved_hash != request_hash(request.method, str(request.url), final):
            await _refuse(
                session,
                row,
                ctx.user_id,
                BotaiError(ErrorCode.ACTION_CHANGED, "Запрос изменился после подтверждения"),
            )
        try:
            check_request(list(conn.allow_hosts), request.method, str(request.url), proposal.intent)
        except BotaiError as error:
            await _refuse(session, row, ctx.user_id, error)

        chosen = executor or get_executor()
        if chosen is None:
            row.state = HANDED_OFF
            await session.commit()
            block = HandoffBlock(
                id=new_id(),
                at=now,
                title=proposal.intent[:200],
                url=str(request.url),  # type: ignore[arg-type]
                text="\n".join(
                    [proposal.intent, ""] + [f"{f.label}: {f.value}" for f in request.fields]
                )[:8000],
            )
            await ctx.emit(block)
            return {"state": HANDED_OFF}

        row.executed_at = now  # claimed before sending: a retried job cannot send twice
        await session.commit()
        try:
            outcome = await chosen.execute(
                ctx,
                allow_hosts=list(conn.allow_hosts),
                method=request.method,
                url=str(request.url),
                body_kind=request.body_kind,
                fields=final,
            )
        except Exception:
            log.exception("action %s failed", proposal_id)
            outcome = ExecutionResult(False, None, "Не удалось отправить запрос")
        row.state = EXECUTED if outcome.ok else FAILED
        row.result = {
            **(row.result or {}),
            "ok": outcome.ok,
            "status": outcome.status,
        }
        await _audit(
            session,
            ctx.user_id,
            "action_executed" if outcome.ok else "action_failed",
            {
                "proposal_id": proposal_id,
                "connection_id": row.connection_id,
                "method": request.method,
                "url": str(request.url),
                "approved_hash": approved_hash,
                "status": outcome.status,
            },
        )
        await session.commit()
    await ctx.emit(TextBlock(id=new_id(), at=now, markdown=outcome.message[:8000]))
    return {"state": EXECUTED if outcome.ok else FAILED, "status": outcome.status}
