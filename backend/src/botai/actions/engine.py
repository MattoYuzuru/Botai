"""The action engine: policy check, journal, ask, exactly-once execution.

One action goes through four steps (D-006, D-026):

1. **prepare** — infer the class, resolve the owner policy (and its limits), derive the
   idempotency key, write one `actions_log` row with the redacted proposal. The row's decision is
   `auto` (allowed), `asked` (waiting for the owner) or `never` (refused). A repeated key returns
   the existing row instead of creating a second action.
2. **ask** — for `asked`, the engine calls the caller's `AskUser` callback (the job's existing
   ask channel: a `ChoiceBlock` with "Разрешить/Запретить", so the job parks and SSE/answers work)
   and records the decision. Without a callback the action stays `asked` and the caller gets
   `approval_required`; it can be decided later by the API and executed by a retry with the same
   key. The journal deliberately has no body/header values, so an approved action can only be
   executed by a caller that still holds the request (the run that proposed it).
3. **execute** — one atomic claim moves `auto`/`approved` to `executing`; a second concurrent or
   retried call cannot send again. The journal then gets the request summary (method, URL, body
   hash) and the response summary.
4. **history** — the owner's journal, newest first.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.actions.executor import ActionExecutor, ExecutionOutcome
from botai.actions.models import ActionClass, ActionMode, ActionRequest, ScopeKind
from botai.actions.policy import (
    ResolvedPolicy,
    effective_policies,
    enforce_limits,
    infer_action_class,
    normalize_site,
    resolve_policy,
)
from botai.agent.v4.protocol import AskQuestion, AskUser
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import (
    ACTION_CLASSES,
    ACTION_MODES,
    POLICY_SCOPE_KINDS,
    ActionLog,
    ActionPolicy,
)

Clock = Callable[[], datetime]
"""A `() -> datetime` callable (the wiring passes `services.clock.now`, as the runtime does)."""

APPROVAL_OPTIONS: tuple[str, ...] = ("Разрешить", "Запретить")
TERMINAL_DECISIONS = ("executed", "failed", "denied", "never", "expired")
ACTION_QUESTION_MAX = 4000
HISTORY_LIMIT_MAX = 200


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One `actions_log` row as the engine/API/tools see it (values are already redacted)."""

    id: int
    decision: str
    mode: ActionMode
    action_class: ActionClass
    scope_kind: ScopeKind
    scope_id: str | None
    intent: str
    proposal: dict[str, Any]
    request: dict[str, Any] | None
    result: dict[str, Any] | None
    rollback: dict[str, Any] | None
    idempotency_key: str | None
    run_id: str | None
    job_id: str | None
    source_id: str | None
    created_at: datetime
    decided_at: datetime | None
    executed_at: datetime | None

    @classmethod
    def of(cls, row: ActionLog) -> JournalEntry:
        return cls(
            id=row.id,
            decision=row.decision,
            mode=row.mode,  # type: ignore[arg-type]
            action_class=row.action_class,  # type: ignore[arg-type]
            scope_kind=row.scope_kind,  # type: ignore[arg-type]
            scope_id=row.scope_id,
            intent=row.intent,
            proposal=dict(row.proposal or {}),
            request=dict(row.request) if row.request is not None else None,
            result=dict(row.result) if row.result is not None else None,
            rollback=dict(row.rollback) if row.rollback is not None else None,
            idempotency_key=row.idempotency_key,
            run_id=row.run_id,
            job_id=row.job_id,
            source_id=row.source_id,
            created_at=row.created_at,
            decided_at=row.decided_at,
            executed_at=row.executed_at,
        )

    def public(self) -> dict[str, Any]:
        """The journal row for the owner (no secret values: proposal/request were redacted)."""
        return {
            "id": self.id,
            "decision": self.decision,
            "mode": self.mode,
            "action_class": self.action_class,
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "intent": self.intent,
            "proposal": self.proposal,
            "request": self.request,
            "result": self.result,
            "rollback": self.rollback,
            "idempotency_key": self.idempotency_key,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "source_id": self.source_id,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
        }


@dataclass(frozen=True, slots=True)
class PreparedAction:
    """A journaled proposal and the resolved policy; `duplicate` means the key was already used."""

    entry: JournalEntry
    request: ActionRequest
    policy: ResolvedPolicy
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class ActionReport:
    """The result of actually running a proposal: terminal status + the journal row."""

    status: Literal[
        "executed", "failed", "denied", "never", "expired", "approval_required", "pending"
    ]
    entry: JournalEntry
    outcome: ExecutionOutcome | None = None
    duplicate: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "executed"

    def model_data(self) -> dict[str, Any]:
        """The tool-facing view: what was proposed, what happened, nothing secret."""
        data = self.entry.public()
        data["action_id"] = self.entry.id
        data["status"] = self.status
        data["duplicate"] = self.duplicate
        if self.outcome is not None:
            data["execution"] = self.outcome.echo
        return data


class ActionEngine:
    """Engine of one process: stateless per call, its DB session factory is injected."""

    def __init__(
        self,
        *,
        db: async_sessionmaker[AsyncSession],
        clock: Clock,
        http_transport: Any = None,
        executor: ActionExecutor | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._executor = executor or ActionExecutor(http_transport=http_transport)

    # -- policies ------------------------------------------------------------------------------

    async def list_policies(self, user_id: str) -> list[ActionPolicy]:
        async with self._db() as session:
            rows = await session.execute(
                select(ActionPolicy)
                .where(ActionPolicy.user_id == user_id)
                .order_by(ActionPolicy.scope_kind, ActionPolicy.action_class)
            )
            return list(rows.scalars())

    async def upsert_policy(
        self,
        *,
        user_id: str,
        scope_kind: ScopeKind,
        scope_id: str | None,
        action_class: ActionClass,
        mode: ActionMode,
        limits: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> ActionPolicy:
        """Create or update one policy row; scope/id/class/mode/limits are validated here."""
        if scope_kind not in POLICY_SCOPE_KINDS:
            raise BotaiError(ErrorCode.VALIDATION, f"Неизвестная область политики: {scope_kind}")
        if action_class not in ACTION_CLASSES:
            raise BotaiError(ErrorCode.VALIDATION, f"Неизвестный класс действия: {action_class}")
        if mode not in ACTION_MODES:
            raise BotaiError(ErrorCode.VALIDATION, f"Неизвестный режим: {mode}")
        normalized = _normalize_scope(scope_kind, scope_id)
        clean_limits = _validated_limits(limits)
        now = self._clock()
        async with self._db() as session:
            existing = await self._policy_row(
                session,
                user_id=user_id,
                scope_kind=scope_kind,
                scope_id=normalized,
                action_class=action_class,
            )
            if existing is not None:
                existing.mode = mode
                existing.limits = clean_limits
                existing.note = note
                existing.updated_at = now
                await session.commit()
                await session.refresh(existing)
                return existing
            row = ActionPolicy(
                user_id=user_id,
                scope_kind=scope_kind,
                scope_id=normalized,
                action_class=action_class,
                mode=mode,
                limits=clean_limits,
                note=note,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def delete_policy(self, *, user_id: str, policy_id: str) -> None:
        async with self._db() as session:
            row = await session.get(ActionPolicy, policy_id)
            if row is None or row.user_id != user_id:
                raise BotaiError(ErrorCode.NOT_FOUND, "Политика не найдена")
            await session.delete(row)
            await session.commit()

    async def effective(
        self,
        *,
        user_id: str,
        url: str | None = None,
        source_id: str | None = None,
        space_id: str | None = None,
    ) -> list[ResolvedPolicy]:
        async with self._db() as session:
            return await effective_policies(
                session, user_id=user_id, url=url, source_id=source_id, space_id=space_id
            )

    # -- journal -------------------------------------------------------------------------------

    async def get(self, *, user_id: str, action_id: int) -> JournalEntry:
        async with self._db() as session:
            row = await self._owned(session, user_id, action_id)
            return JournalEntry.of(row)

    async def history(
        self,
        *,
        user_id: str,
        source_id: str | None = None,
        action_class: str | None = None,
        decision: str | None = None,
        limit: int = 50,
    ) -> list[JournalEntry]:
        limit = max(1, min(limit, HISTORY_LIMIT_MAX))
        query = select(ActionLog).where(ActionLog.user_id == user_id)
        if source_id is not None:
            query = query.where(ActionLog.source_id == source_id)
        if action_class is not None:
            query = query.where(ActionLog.action_class == action_class)
        if decision is not None:
            query = query.where(ActionLog.decision == decision)
        query = query.order_by(ActionLog.created_at.desc(), ActionLog.id.desc()).limit(limit)
        async with self._db() as session:
            rows = await session.execute(query)
            return [JournalEntry.of(row) for row in rows.scalars()]

    async def prepare(
        self,
        *,
        user_id: str,
        request: ActionRequest,
        run_id: str | None = None,
        job_id: str | None = None,
    ) -> PreparedAction:
        """Journal the proposal and resolve its policy. Never executes anything."""
        now = self._clock()
        action_class = infer_action_class(
            transport=request.transport,
            method=request.method,
            url=request.url,
            intent=request.intent,
            declared=request.action_class,
            request=request,
        )
        key = request.idempotency_key or (f"run:{run_id or 'action'}:{request.canonical_hash()}")
        async with self._db() as session:
            resolved = await resolve_policy(
                session,
                user_id=user_id,
                action_class=action_class,
                url=request.url,
                source_id=request.source_id,
                space_id=request.space_id,
            )
            resolved = await enforce_limits(session, user_id=user_id, resolved=resolved, now=now)
            existing = await self._by_key(session, user_id=user_id, key=key)
            if existing is not None:
                return PreparedAction(
                    entry=JournalEntry.of(existing),
                    request=request,
                    policy=resolved,
                    duplicate=True,
                )
            decision = _initial_decision(resolved.mode)
            row = ActionLog(
                user_id=user_id,
                run_id=run_id,
                job_id=job_id,
                source_id=request.source_id,
                scope_kind=resolved.scope_kind,
                scope_id=resolved.scope_id,
                action_class=action_class,
                mode=resolved.mode,
                decision=decision,
                intent=request.intent,
                proposal={**request.public_preview(), "policy": resolved.public()},
                idempotency_key=key,
                rollback={"hint": request.undo_hint} if request.undo_hint else None,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await self._by_key(session, user_id=user_id, key=key)
                if existing is None:  # pragma: no cover - only a non-key constraint can do this
                    raise
                return PreparedAction(
                    entry=JournalEntry.of(existing),
                    request=request,
                    policy=resolved,
                    duplicate=True,
                )
            await session.refresh(row)
            return PreparedAction(entry=JournalEntry.of(row), request=request, policy=resolved)

    # -- decide + execute ----------------------------------------------------------------------

    async def run(
        self,
        *,
        user_id: str,
        request: ActionRequest,
        run_id: str | None = None,
        job_id: str | None = None,
        ask: AskUser | None = None,
        control: Any = None,
    ) -> ActionReport:
        """Prepare, ask when the policy says so, execute when allowed. The one tool entry point."""
        prepared = await self.prepare(
            user_id=user_id, request=request, run_id=run_id, job_id=job_id
        )
        entry = prepared.entry
        if entry.decision in TERMINAL_DECISIONS:
            return ActionReport(
                status=entry.decision,  # type: ignore[arg-type]
                entry=entry,
                duplicate=prepared.duplicate,
            )
        if entry.decision == "asked":
            if ask is None:
                return ActionReport(status="approval_required", entry=entry)
            answer = await ask(
                AskQuestion(
                    question=approval_question(request, prepared.policy),
                    options=APPROVAL_OPTIONS,
                    input_kind="choice",
                    timeout_s=1800.0,
                )
            )
            approved = not answer.cancelled and answer.choice == 0
            entry = await self.decide(user_id=user_id, action_id=entry.id, approve=approved)
            if not approved:
                return ActionReport(status="denied", entry=entry)
        if entry.decision in ("auto", "approved"):
            return await self._execute(
                user_id=user_id,
                action_id=entry.id,
                request=request,
                control=control,
                duplicate=prepared.duplicate,
            )
        return ActionReport(status="pending", entry=entry)

    async def execute_prepared(
        self, prepared: PreparedAction, *, user_id: str, control: Any = None
    ) -> ActionReport:
        """Execute an already decided proposal (the v3 ACTION bridge uses this after approval).

        Unlike `run`, it never asks: the caller made the decision through its own channel (the v3
        `ApprovalBlock`). A still-`asked` row is a conflict, not an auto-approval.
        """
        entry = prepared.entry
        if entry.decision in TERMINAL_DECISIONS:
            return ActionReport(
                status=entry.decision,  # type: ignore[arg-type]
                entry=entry,
                duplicate=prepared.duplicate,
            )
        if entry.decision == "asked":
            raise BotaiError(
                ErrorCode.CONFLICT,
                "Действие ещё не подтверждено: сначала нужно решение владельца",
            )
        if entry.decision == "executing":
            return ActionReport(status="pending", entry=entry, duplicate=prepared.duplicate)
        return await self._execute(
            user_id=user_id,
            action_id=entry.id,
            request=prepared.request,
            control=control,
            duplicate=prepared.duplicate,
        )

    async def decide(self, *, user_id: str, action_id: int, approve: bool) -> JournalEntry:
        """Record the owner's decision on an `asked` action (API and the ask channel route here)."""
        now = self._clock()
        async with self._db() as session:
            row = await self._owned(session, user_id, action_id)  # a foreign row is 404
            if row.decision != "asked":
                raise BotaiError(
                    ErrorCode.CONFLICT, "Это действие уже обработано или не ждёт решения"
                )
            decided = await session.execute(
                update(ActionLog)
                .where(
                    ActionLog.id == action_id,
                    ActionLog.user_id == user_id,
                    ActionLog.decision == "asked",
                )
                .values(
                    decision="approved" if approve else "denied",
                    decided_at=now,
                    updated_at=now,
                )
                .returning(ActionLog.id)
            )
            if decided.scalar_one_or_none() is None:
                raise BotaiError(
                    ErrorCode.CONFLICT, "Это действие уже обработано или не ждёт решения"
                )
            await session.commit()
            return JournalEntry.of(await self._owned(session, user_id, action_id))

    async def _execute(
        self,
        *,
        user_id: str,
        action_id: int,
        request: ActionRequest,
        control: Any,
        duplicate: bool = False,
    ) -> ActionReport:
        now = self._clock()
        async with self._db() as session:
            claimed = await session.execute(
                update(ActionLog)
                .where(
                    ActionLog.id == action_id,
                    ActionLog.user_id == user_id,
                    ActionLog.decision.in_(("auto", "approved")),
                )
                .values(decision="executing", updated_at=now)
                .returning(ActionLog.id)
            )
            claimed_id = claimed.scalar_one_or_none()
            await session.commit()
            row = await self._owned(session, user_id, action_id)
            entry = JournalEntry.of(row)
        if claimed_id is None:
            # Another executor claimed it (or it finished between our read and write). Report the
            # current row instead of sending a second request.
            status = entry.decision if entry.decision in TERMINAL_DECISIONS else "pending"
            return ActionReport(status=status, entry=entry, duplicate=duplicate)  # type: ignore[arg-type]

        outcome = await self._executor.execute(request, control=control, run_id=entry.run_id or "")
        decision = "executed" if outcome.ok else "failed"
        async with self._db() as session:
            await session.execute(
                update(ActionLog)
                .where(
                    ActionLog.id == action_id,
                    ActionLog.user_id == user_id,
                    ActionLog.decision == "executing",
                )
                .values(
                    decision=decision,
                    request=request.request_summary(),
                    result=outcome.journal_payload(),
                    executed_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
            row = await self._owned(session, user_id, action_id)
            entry = JournalEntry.of(row)
        return ActionReport(
            status="executed" if outcome.ok else "failed",
            entry=entry,
            outcome=outcome,
            duplicate=duplicate,
        )

    # -- internals -----------------------------------------------------------------------------

    async def _owned(self, session: AsyncSession, user_id: str, action_id: int) -> ActionLog:
        row = await session.get(ActionLog, action_id)
        if row is None or row.user_id != user_id:
            raise BotaiError(ErrorCode.NOT_FOUND, "Действие не найдено")
        return row

    async def _by_key(self, session: AsyncSession, *, user_id: str, key: str) -> ActionLog | None:
        return (
            await session.execute(
                select(ActionLog).where(
                    ActionLog.user_id == user_id, ActionLog.idempotency_key == key
                )
            )
        ).scalar_one_or_none()

    async def _policy_row(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        scope_kind: str,
        scope_id: str | None,
        action_class: str,
    ) -> ActionPolicy | None:
        query = select(ActionPolicy).where(
            ActionPolicy.user_id == user_id,
            ActionPolicy.scope_kind == scope_kind,
            ActionPolicy.action_class == action_class,
        )
        if scope_kind == "global":
            query = query.where(ActionPolicy.scope_id.is_(None))
        else:
            query = query.where(ActionPolicy.scope_id == scope_id)
        return (await session.execute(query)).scalar_one_or_none()


def approval_question(request: ActionRequest, policy: ResolvedPolicy) -> str:
    """What the owner sees in ask mode: exactly the request, its class and its consequences."""
    preview = request.public_preview()
    lines = [f"Агент просит разрешить действие: {request.intent}", ""]
    scope = f"{policy.scope_kind}" + (f" {policy.scope_id}" if policy.scope_id else "")
    lines.append(f"Класс действия: {policy.action_class} (политика: {policy.mode}, {scope}).")
    if policy.escalated:
        lines.append(f"Причина вопроса: {policy.escalated}.")
    if request.transport == "http":
        lines.append(f"Запрос: {request.method} {request.url}")
        if preview.get("header_names"):
            lines.append("Заголовки: " + ", ".join(preview["header_names"]))
        fields = preview.get("fields")
        if isinstance(fields, list):
            for item in fields:
                lines.append(f"  {item['name']}: {item['value']}")
        elif "body" in preview:
            lines.append(f"Тело: {preview['body']}")
    else:
        for index, step in enumerate(preview.get("steps") or [], start=1):
            parts = [f"{index}. {step.get('action')}"]
            if step.get("url"):
                parts.append(step["url"])
            if step.get("target"):
                parts.append(str(step["target"]))
            if "text" in step:
                parts.append(f"«{step['text']}»")
            if "key" in step:
                parts.append(step["key"])
            if "value" in step:
                parts.append(str(step["value"]))
            lines.append(" ".join(parts))
    if request.undo_hint:
        lines.append(f"Обратное действие: {request.undo_hint}")
    else:
        lines.append("Автоматического отката нет: действие будет выполнено как показано.")
    return "\n".join(lines)[:ACTION_QUESTION_MAX]


def _initial_decision(mode: ActionMode) -> str:
    if mode == "never":
        return "never"
    return "auto" if mode == "auto" else "asked"


def _normalize_scope(scope_kind: str, scope_id: str | None) -> str | None:
    if scope_kind == "global":
        if scope_id is not None:
            raise BotaiError(ErrorCode.VALIDATION, "Глобальная политика не имеет scope_id")
        return None
    if not scope_id:
        raise BotaiError(ErrorCode.VALIDATION, "Для этой области нужен scope_id")
    if scope_kind == "site":
        site = normalize_site(scope_id if "://" in scope_id else f"https://{scope_id}")
        if not site:
            raise BotaiError(ErrorCode.VALIDATION, "Сайт должен быть доменом или URL")
        return site
    if len(scope_id) != 26:
        raise BotaiError(
            ErrorCode.VALIDATION, "scope_id пространства/источника — ULID (26 символов)"
        )
    return scope_id


def _validated_limits(limits: dict[str, Any] | None) -> dict[str, int]:
    if not limits:
        return {}
    allowed = ("max_per_hour", "max_per_day")
    unknown = sorted(set(limits) - set(allowed))
    if unknown:
        raise BotaiError(ErrorCode.VALIDATION, f"Неизвестные лимиты: {', '.join(unknown)}")
    clean: dict[str, int] = {}
    for key in allowed:
        value = limits.get(key)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise BotaiError(ErrorCode.VALIDATION, f"Лимит {key} должен быть положительным целым")
        clean[key] = value
    return clean


async def history_count(db: async_sessionmaker[AsyncSession], *, user_id: str) -> int:
    """Small helper for tests/status: how many actions are journaled for the user."""
    async with db() as session:
        return int(
            (
                await session.scalar(
                    select(func.count()).select_from(ActionLog).where(ActionLog.user_id == user_id)
                )
            )
            or 0
        )


__all__ = [
    "ACTION_QUESTION_MAX",
    "APPROVAL_OPTIONS",
    "HISTORY_LIMIT_MAX",
    "TERMINAL_DECISIONS",
    "ActionEngine",
    "ActionReport",
    "JournalEntry",
    "PreparedAction",
    "approval_question",
    "history_count",
]
