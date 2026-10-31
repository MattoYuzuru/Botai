"""Owner autonomy policy: which class of action is `auto`, `ask` or `never` for which scope.

D-006/D-025: there is no architectural ban on writes; the owner decides. Defaults are documented
in `DEFAULT_MODES`: reads are free, everything that writes or spends asks first. Destructive and
money-default `ask` is a policy default, not a wall: the owner can set `auto` or `never` per
source/space/site/globally.

Resolution is the narrowest matching row: source > space > site > global. A policy row is per
`action_class`; classes without a row use the default. `limits` escalates `auto` to `ask` when the
counter is reached instead of silently refusing the action.

The action class is inferred from the request, and a class named by the model can only raise the
inferred one — never lower it (`infer_action_class`), so a `DELETE` cannot be proposed as
`write_soft` to dodge the owner's policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.actions.models import ActionClass, ActionMode, ActionRequest, ScopeKind
from botai.agent.v4.protocol import PolicyDecision, PolicyRequest
from botai.db.models import ACTION_CLASSES, ActionLog, ActionPolicy

# Default modes by class: no row means this (docs/v4/02-architecture.md §7).
DEFAULT_MODES: dict[ActionClass, ActionMode] = {
    "read": "auto",
    "write_soft": "ask",
    "write_destructive": "ask",
    "money": "ask",
    "auth": "ask",
}

# The narrower the scope, the stronger its row (source beats space beats site beats global).
SCOPE_RANK: dict[ScopeKind, int] = {"global": 0, "site": 1, "space": 2, "source": 3}

# Severity order for the "the model cannot lower a class" rule.
CLASS_RANK: dict[ActionClass, int] = {
    "read": 0,
    "write_soft": 1,
    "write_destructive": 2,
    "money": 3,
    "auth": 4,
}

LIMIT_KEYS = ("max_per_hour", "max_per_day")

# Intent/URL markers that raise a proposal above its method-based class. English and Russian:
# the owner's tasks are in Russian, the sites are mixed.
_DESTRUCTIVE = re.compile(
    r"(?i)\b(delete|remove|erase|revoke|withdraw|unsubscribe|deactivate|deauthorize|"
    r"final[-_ ]?submit|drop[-_ ]?course)\b|удал|отозв|отмен[аи]|отпис|сдать|сдач|исключ|"
    r"расторгн|деактив"
)
_MONEY = re.compile(
    r"(?i)\b(pay|payment|purchase|checkout|billing|top[-_ ]?up|donate|invoice|fee)\b|"
    r"оплат|плат[её]ж|покуп|взнос|оплатить|стоимость|цена|тариф|донат"
)
_AUTH = re.compile(
    r"(?i)\b(password|passwd|passphrase|otp|mfa|two[-_ ]?factor|2fa|logout|log[-_ ]?out|"
    r"sign[-_ ]?out|email[-_ ]?change|change[-_ ]?email)\b|парол|выход|смен[аы] почт|"
    r"сменить почт|двухфактор"
)


def normalize_site(url: str | None) -> str | None:
    """The policy `site` key of a URL: lower-case host without port or credentials."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.hostname
    return host.lower() if host else None


def _haystack(transport: str, intent: str, url: str | None, request: ActionRequest | None) -> str:
    parts = [intent, url or ""]
    if request is not None:
        if request.transport == "http" and request.method:
            parts.append(request.method)
        for step in request.steps:
            if step.url:
                parts.append(step.url)
            if step.target:
                parts.extend(str(value) for value in step.target.values())
            if step.label:
                parts.append(step.label)
            if step.value:
                parts.append(step.value)
    return " ".join(parts)


def infer_action_class(
    *,
    transport: Literal["http", "browser"] = "http",
    method: str | None = None,
    url: str | None = None,
    intent: str = "",
    declared: ActionClass | None = None,
    request: ActionRequest | None = None,
) -> ActionClass:
    """Infer the action class, never below what the request itself implies.

    Base: `GET`-like is `read`, `DELETE` is destructive, any other write method is `write_soft`,
    a browser action is `write_soft` (a click can submit anything). Intent/URL markers raise it to
    `write_destructive`, `money` or `auth`. `declared` may only raise the result, never lower it.
    """
    upper = (method or "").strip().upper()
    if transport == "browser":
        base: ActionClass = "write_soft"
    elif upper in ("GET", "HEAD", "OPTIONS", "TRACE"):
        base = "read"
    elif upper == "DELETE":
        base = "write_destructive"
    else:
        base = "write_soft"

    text = _haystack(transport, intent, url, request)
    inferred = base
    if _AUTH.search(text):
        inferred = _higher(inferred, "auth")
    if _MONEY.search(text):
        inferred = _higher(inferred, "money")
    if _DESTRUCTIVE.search(text):
        inferred = _higher(inferred, "write_destructive")
    if declared is not None:
        inferred = _higher(inferred, declared)
    return inferred


def _higher(left: ActionClass, right: ActionClass) -> ActionClass:
    return left if CLASS_RANK[left] >= CLASS_RANK[right] else right


@dataclass(frozen=True, slots=True)
class ResolvedPolicy:
    """The effective decision for one class in one scope: mode, scope and limits."""

    action_class: ActionClass
    mode: ActionMode
    scope_kind: ScopeKind = "global"
    scope_id: str | None = None
    limits: dict[str, int] | None = None
    escalated: str | None = None
    policy_id: str | None = None

    @property
    def limit_map(self) -> dict[str, int]:
        return dict(self.limits or {})

    def public(self) -> dict[str, Any]:
        return {
            "action_class": self.action_class,
            "mode": self.mode,
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "limits": self.limit_map,
            "escalated": self.escalated,
        }


def _clean_limits(raw: Any) -> dict[str, int]:
    limits: dict[str, int] = {}
    if not isinstance(raw, dict):
        return limits
    for key in LIMIT_KEYS:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            limits[key] = value
    return limits


def _scope_matches(
    row: ActionPolicy,
    *,
    site: str | None,
    source_id: str | None,
    space_id: str | None,
) -> bool:
    kind = row.scope_kind
    if kind == "global":
        return True
    if kind == "site":
        return site is not None and row.scope_id == site
    if kind == "space":
        return space_id is not None and row.scope_id == space_id
    return source_id is not None and row.scope_id == source_id


async def resolve_policy(
    session: AsyncSession,
    *,
    user_id: str,
    action_class: ActionClass,
    url: str | None = None,
    source_id: str | None = None,
    space_id: str | None = None,
) -> ResolvedPolicy:
    """The narrowest owner row for the class, or the documented default without one."""
    rows = (
        await session.execute(
            select(ActionPolicy).where(
                ActionPolicy.user_id == user_id,
                ActionPolicy.action_class == action_class,
            )
        )
    ).scalars()
    site = normalize_site(url)
    matches = [
        row
        for row in rows
        if _scope_matches(row, site=site, source_id=source_id, space_id=space_id)
    ]
    if not matches:
        return ResolvedPolicy(action_class=action_class, mode=DEFAULT_MODES[action_class])
    chosen = max(matches, key=lambda row: SCOPE_RANK[row.scope_kind])  # type: ignore[index]
    return ResolvedPolicy(
        action_class=action_class,
        mode=chosen.mode,  # type: ignore[arg-type]
        scope_kind=chosen.scope_kind,  # type: ignore[arg-type]
        scope_id=chosen.scope_id,
        limits=_clean_limits(chosen.limits),
        policy_id=chosen.id,
    )


async def _executed_count(
    session: AsyncSession,
    *,
    user_id: str,
    action_class: str,
    since: datetime,
) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(ActionLog)
        .where(
            ActionLog.user_id == user_id,
            ActionLog.action_class == action_class,
            ActionLog.decision == "executed",
            ActionLog.created_at >= since,
        )
    )
    return int(total or 0)


async def enforce_limits(
    session: AsyncSession,
    *,
    user_id: str,
    resolved: ResolvedPolicy,
    now: datetime,
) -> ResolvedPolicy:
    """Escalate `auto` to `ask` when the owner's per-class counter is reached (D-006 limits)."""
    limits = resolved.limit_map
    if resolved.mode != "auto" or not limits:
        return resolved
    windows = (
        ("max_per_hour", now - timedelta(hours=1)),
        ("max_per_day", now - timedelta(days=1)),
    )
    for key, since in windows:
        limit = limits.get(key)
        if limit is None:
            continue
        used = await _executed_count(
            session, user_id=user_id, action_class=resolved.action_class, since=since
        )
        if used >= limit:
            return replace(
                resolved,
                mode="ask",
                escalated=(
                    f"лимит {key}={limit} для класса {resolved.action_class} исчерпан "
                    f"({used} выполнено): нужно подтверждение"
                ),
            )
    return resolved


async def effective_policies(
    session: AsyncSession,
    *,
    user_id: str,
    url: str | None = None,
    source_id: str | None = None,
    space_id: str | None = None,
) -> list[ResolvedPolicy]:
    """The resolved mode for every action class in one scope (what the UI shows)."""
    return [
        await resolve_policy(
            session,
            user_id=user_id,
            action_class=action_class,  # type: ignore[arg-type]
            url=url,
            source_id=source_id,
            space_id=space_id,
        )
        for action_class in ACTION_CLASSES
    ]


# --- runtime PolicyDecider adapter -------------------------------------------------------------


class ActionPolicyDecider:
    """The runtime's `PolicyDecider` for generic write tools (`docs/v4/11-runtime.md` §3).

    The runtime asks it before a tool with `policy != "read"`. Its only hard power is refusing an
    explicit `never`; `ask` cannot block a synchronous callback (there is no question channel in
    `PolicyRequest`), so those calls are allowed here and the tool that owns the action asks the
    owner itself — `action_execute` does exactly that through the job's ask channel. Anything this
    decider lets through still lands in the tool's own journal/audit where the tool has one.
    """

    def __init__(
        self,
        db: async_sessionmaker[AsyncSession],
        *,
        user_id: str,
    ) -> None:
        self._db = db
        self._user_id = user_id

    async def __call__(self, request: PolicyRequest) -> PolicyDecision:
        policy = getattr(request, "policy", "read")
        arguments = getattr(request, "arguments", {}) or {}
        action_class: ActionClass = policy if policy in ACTION_CLASSES else "write_soft"  # type: ignore[assignment]
        url = arguments.get("url") if isinstance(arguments.get("url"), str) else None
        method = arguments.get("method") if isinstance(arguments.get("method"), str) else None
        inferred = infer_action_class(
            transport="http",
            method=method or ("POST" if action_class != "read" else "GET"),
            url=url,
            intent=str(arguments.get("intent") or ""),
            declared=action_class,
        )
        source_id = (
            arguments.get("source_id") if isinstance(arguments.get("source_id"), str) else None
        )
        space_id = arguments.get("space_id") if isinstance(arguments.get("space_id"), str) else None
        async with self._db() as session:
            resolved = await resolve_policy(
                session,
                user_id=self._user_id,
                action_class=inferred,
                url=url,
                source_id=source_id,
                space_id=space_id,
            )
        if resolved.mode == "never":
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"Действия класса {inferred} запрещены политикой владельца "
                    f"({resolved.scope_kind})."
                ),
            )
        return PolicyDecision()


def build_policy_decider(
    db: async_sessionmaker[AsyncSession] | None, *, user_id: str
) -> ActionPolicyDecider | None:
    """A decider for the v4 runtime, or `None` when the services have no DB (unit tests)."""
    return ActionPolicyDecider(db, user_id=user_id) if db is not None else None


__all__ = [
    "CLASS_RANK",
    "DEFAULT_MODES",
    "LIMIT_KEYS",
    "SCOPE_RANK",
    "ActionPolicyDecider",
    "ResolvedPolicy",
    "build_policy_decider",
    "effective_policies",
    "enforce_limits",
    "infer_action_class",
    "normalize_site",
    "resolve_policy",
]
