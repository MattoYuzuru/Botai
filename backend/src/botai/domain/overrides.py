"""The student's corrections of task statuses (D-045, docs/v4/27-study-hub-grades-fastpath.md §4).

Two deterministic levels, both applied without a model:

* a status rule — «in this source „Решение прикреплено“ means submitted» — maps a normalized
  `deadlines.state_text` to a state, for one source or for every source of the user (`source_id`
  NULL). The source's own rule wins over an all-sources one. A sync applies the rules when it
  writes deadlines (`sources/v4/domain.py`); setting or deleting a rule recomputes the stored rows
  at once;
* a mark on one task (`deadlines.user_state`): stronger than the source and than a rule, cleared
  only explicitly.

The effective state of a task is `user_state`, else `state` (which already holds the rule);
`state_source` tells which of the three decided it. `changed_at` is not touched by a rule (it is
the source's change, not the student's); a mark sets it, so the feed shows the student's own move.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Literal

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.clock import SystemClock
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import STATUS_RULE_STATES, Deadline, Source, StatusRule
from botai.sources.states import task_state

StateSource = Literal["student", "rule", "source"]
USER_STATES: tuple[str, ...] = ("open", "submitted", "graded")
MAX_LABEL = 120

# (source_id or None, normalized label) -> state
RuleMap = Mapping[tuple[str | None, str], str]


def normalize_label(text: str | None) -> str:
    """The comparable form of a status text: casefolded, ё as е, whitespace collapsed."""
    return " ".join((text or "").casefold().replace("ё", "е").split())[:MAX_LABEL]


def rule_state(rules: RuleMap, source_id: str | None, state_text: str | None) -> str | None:
    """The state a rule gives a task of `source_id` with `state_text`; the source's rule first."""
    label = normalize_label(state_text)
    if not label:
        return None
    if source_id is not None and (found := rules.get((source_id, label))) is not None:
        return found
    return rules.get((None, label))


def effective_state(
    *, user_state: str | None, state: str, state_text: str | None, rule: str | None
) -> tuple[str, StateSource]:
    """What the student sees and who decided it: the mark, the rule, else the source."""
    if user_state is not None:
        return user_state, "student"
    if rule is not None and rule == state and state_text:
        return state, "rule"
    return state, "source"


async def rules_of(
    session: AsyncSession, user_id: str, *, source_ids: Iterable[str] | None = None
) -> dict[tuple[str | None, str], str]:
    """The user's rules as a lookup map; `source_ids` narrows the per-source ones (the
    all-sources rules always come along)."""
    stmt = select(StatusRule.source_id, StatusRule.label, StatusRule.state).where(
        StatusRule.user_id == user_id
    )
    if source_ids is not None:
        wanted = sorted(set(source_ids))
        stmt = stmt.where(
            or_(StatusRule.source_id.is_(None), StatusRule.source_id.in_(wanted))
            if wanted
            else StatusRule.source_id.is_(None)
        )
    rows = (await session.execute(stmt)).all()
    return {(source_id, label): state for source_id, label, state in rows}


# --- status rules -------------------------------------------------------------------------------


def _fail(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, detail)


async def _owned_source(session: AsyncSession, user_id: str, source_id: str) -> Source:
    source = await session.scalar(
        select(Source).where(Source.id == source_id, Source.user_id == user_id)
    )
    if source is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Источник не найден")
    return source


async def list_status_rules(session: AsyncSession, user_id: str) -> list[StatusRule]:
    """The user's rules, newest first."""
    rows = await session.scalars(
        select(StatusRule)
        .where(StatusRule.user_id == user_id)
        .order_by(StatusRule.created_at.desc(), StatusRule.id.desc())
    )
    return list(rows)


async def _recompute(
    session: AsyncSession, user_id: str, *, source_id: str | None, label: str
) -> int:
    """Re-derive `state` of the user's v4 deadlines whose status text is `label` (within one
    source, or every source when `source_id` is None) from the rules that remain and the words.
    Returns how many rows changed."""
    stmt = select(Deadline).where(
        Deadline.user_id == user_id,
        Deadline.source_id.is_not(None),
        Deadline.state_text.is_not(None),
    )
    if source_id is not None:
        stmt = stmt.where(Deadline.source_id == source_id)
    rows = [row for row in await session.scalars(stmt) if normalize_label(row.state_text) == label]
    if not rows:
        return 0
    rules = await rules_of(
        session, user_id, source_ids={row.source_id for row in rows if row.source_id}
    )
    changed = 0
    for row in rows:
        state = rule_state(rules, row.source_id, row.state_text) or task_state(row.state_text)
        if row.state != state:
            row.state = state
            changed += 1
    await session.flush()
    return changed


async def set_status_rule(
    session: AsyncSession,
    user_id: str,
    *,
    source_id: str | None,
    label: str,
    state: str,
) -> StatusRule:
    """Create or replace the rule for (source, label) and recompute the stored tasks at once."""
    if state not in STATUS_RULE_STATES:
        raise _fail("Состояние правила: open, submitted, graded или missed")
    normalized = normalize_label(label)
    if not normalized:
        raise _fail("Нужна подпись статуса из источника")
    if source_id is not None:
        await _owned_source(session, user_id, source_id)
    found = await session.scalar(
        select(StatusRule).where(
            StatusRule.user_id == user_id,
            StatusRule.label == normalized,
            StatusRule.source_id.is_(None)
            if source_id is None
            else StatusRule.source_id == source_id,
        )
    )
    if found is None:
        found = StatusRule(user_id=user_id, source_id=source_id, label=normalized, state=state)
        session.add(found)
    else:
        found.state = state
    await session.flush()
    await _recompute(session, user_id, source_id=source_id, label=normalized)
    return found


async def delete_status_rule(session: AsyncSession, user_id: str, rule_id: str) -> None:
    """Remove a rule; the tasks it decided fall back to another rule or to the words."""
    rule = await session.scalar(
        select(StatusRule).where(StatusRule.id == rule_id, StatusRule.user_id == user_id)
    )
    if rule is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Правило не найдено")
    source_id, label = rule.source_id, rule.label
    await session.execute(delete(StatusRule).where(StatusRule.id == rule.id))
    await session.flush()
    await _recompute(session, user_id, source_id=source_id, label=label)


# --- the student's mark on one task -------------------------------------------------------------


async def set_user_state(
    session: AsyncSession,
    user_id: str,
    deadline_id: str,
    state: str | None,
    *,
    now: datetime | None = None,
) -> Deadline:
    """Mark one task (`open` | `submitted` | `graded`) or clear the mark (None)."""
    if state is not None and state not in USER_STATES:
        raise _fail("Отметка задания: open, submitted, graded или пусто")
    row = await session.scalar(
        select(Deadline).where(Deadline.id == deadline_id, Deadline.user_id == user_id)
    )
    if row is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Задание не найдено")
    if row.user_state != state:
        row.user_state = state
        row.changed_at = now or SystemClock().now()
    await session.flush()
    return row
