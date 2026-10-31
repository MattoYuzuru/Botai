"""Owner API of the action policy and journal (D-006, D-025, D-026).

Two groups of endpoints:

* `/action-policies` — the owner's autonomy policy (`auto`/`ask`/`never`) per scope and class:
  list, upsert, delete, and the effective resolution for a URL/source/space.
* `/actions` — the append-only journal: list/get one row and decide a pending `ask` action.
  A decision is recorded, but execution stays with the run that proposed the action: it holds
  the request values, while the journal deliberately keeps only a redacted preview and the body
  hash. The run (or a retry with the same idempotency key) executes an approved row.

All routes are scoped to the current user: a row of another account is `404`, not `403`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from botai.actions.engine import ActionEngine
from botai.api.deps import ServicesDep
from botai.auth.deps import AuthUser
from botai.core.api import ApiModel

router = APIRouter(prefix="/api/v1", tags=["actions"])


def get_action_engine(services: ServicesDep) -> ActionEngine:
    """Per-request engine over the process DB session factory (tests override this)."""
    return ActionEngine(db=services.db, clock=services.clock.now)


EngineDep = Annotated[ActionEngine, Depends(get_action_engine)]


class PolicyIn(ApiModel):
    """Upsert one policy row: (scope_kind, scope_id, action_class) is the identity."""

    scope_kind: Literal["global", "site", "space", "source"]
    scope_id: str | None = None
    action_class: Literal["read", "write_soft", "write_destructive", "money", "auth"]
    mode: Literal["auto", "ask", "never"]
    limits: dict[str, int] | None = None
    note: str | None = None


class PolicyOut(ApiModel):
    id: str
    scope_kind: str
    scope_id: str | None
    action_class: str
    mode: str
    limits: dict[str, Any]
    note: str | None
    updated_at: datetime


class EffectivePolicyOut(ApiModel):
    action_class: str
    mode: str
    scope_kind: str
    scope_id: str | None
    limits: dict[str, int]
    escalated: str | None = None


class ActionOut(ApiModel):
    id: int
    decision: str
    mode: str
    action_class: str
    scope_kind: str
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


class DecisionIn(ApiModel):
    """Resolve an `ask` action: `approve` unblocks execution by the proposing run, `deny` closes it."""

    decision: Literal["approve", "deny"]


@router.get("/action-policies", response_model=list[PolicyOut])
async def list_action_policies(user: AuthUser, engine: EngineDep) -> list[PolicyOut]:
    """All explicit policy rows of the current user."""
    rows = await engine.list_policies(user.id)
    return [PolicyOut.model_validate(row) for row in rows]


@router.put("/action-policies", response_model=PolicyOut)
async def upsert_action_policy(body: PolicyIn, user: AuthUser, engine: EngineDep) -> PolicyOut:
    """Create or update one row. `global` has no `scope_id`; `site` accepts a host or URL."""
    row = await engine.upsert_policy(
        user_id=user.id,
        scope_kind=body.scope_kind,  # type: ignore[arg-type]
        scope_id=body.scope_id,
        action_class=body.action_class,  # type: ignore[arg-type]
        mode=body.mode,  # type: ignore[arg-type]
        limits=body.limits,
        note=body.note,
    )
    return PolicyOut.model_validate(row)


@router.delete("/action-policies/{policy_id}", status_code=204)
async def delete_action_policy(policy_id: str, user: AuthUser, engine: EngineDep) -> None:
    """Delete one row: the class falls back to the documented default for that scope."""
    await engine.delete_policy(user_id=user.id, policy_id=policy_id)


@router.get("/action-policies/effective", response_model=list[EffectivePolicyOut])
async def effective_action_policies(
    user: AuthUser,
    engine: EngineDep,
    url: str | None = Query(default=None, max_length=4096),
    source_id: str | None = Query(default=None, max_length=26),
    space_id: str | None = Query(default=None, max_length=26),
) -> list[EffectivePolicyOut]:
    """The resolved mode of every class for one scope (what would happen to a new action)."""
    resolved = await engine.effective(
        user_id=user.id, url=url, source_id=source_id, space_id=space_id
    )
    return [EffectivePolicyOut.model_validate(item.public()) for item in resolved]


@router.get("/actions", response_model=list[ActionOut])
async def list_actions(
    user: AuthUser,
    engine: EngineDep,
    source_id: str | None = Query(default=None, max_length=26),
    action_class: str | None = Query(default=None, max_length=20),
    decision: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ActionOut]:
    """The action journal, newest first (redacted: no secret values were ever stored)."""
    entries = await engine.history(
        user_id=user.id,
        source_id=source_id,
        action_class=action_class,
        decision=decision,
        limit=limit,
    )
    return [ActionOut(**entry.public()) for entry in entries]


@router.get("/actions/{action_id}", response_model=ActionOut)
async def get_action(action_id: int, user: AuthUser, engine: EngineDep) -> ActionOut:
    """One journal row (proposal, decision, executed request summary, result)."""
    entry = await engine.get(user_id=user.id, action_id=action_id)
    return ActionOut(**entry.public())


@router.post("/actions/{action_id}/decision", response_model=ActionOut)
async def decide_action(
    action_id: int, body: DecisionIn, user: AuthUser, engine: EngineDep
) -> ActionOut:
    """Approve or deny an `ask` action. The decision is journaled; execution belongs to the run.

    The journal intentionally contains no request body/header values, so the API cannot send an
    HTTP action itself: the run that proposed it executes the approved row, and a retry with the
    same idempotency key picks up the decision. `409` when the row is not waiting for a decision.
    """
    entry = await engine.decide(
        user_id=user.id, action_id=action_id, approve=body.decision == "approve"
    )
    return ActionOut(**entry.public())


__all__ = ["router"]
