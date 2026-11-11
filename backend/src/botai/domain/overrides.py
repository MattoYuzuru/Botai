"""The student's corrections of task statuses (D-045, docs/v4/27-study-hub-grades-fastpath.md §4).

Two deterministic levels, both applied without a model:

* a status rule — «in this source „Решение прикреплено“ means submitted» — maps a normalized
  `deadlines.state_text` to a state, for one source or for every source of the user (`source_id`
  NULL). The source's own rule wins over an all-sources one. A sync applies the rules when it
  writes deadlines (`sources/v4/domain.py`); setting or deleting a rule recomputes the stored rows
  at once;
* a mark on one task (`deadlines.user_state`): stronger than the source and than a rule, cleared
  only explicitly. It is written on every copy of the task — the rows of the user's sources with
  the same dedupe key (`domain.keys.deadline_key`) — so every read and every source agree.

The stored `state` is the rule's state, else `source_state` (what the source said at the last
sync, before the rules). The effective state of a task is `user_state`, else `state`;
`state_source` tells which of the three decided it. `changed_at` means "the source changed it":
neither a rule nor a mark touches it.

A recompute takes the same per-source advisory lock as a sync's domain write
(`sources.normalize.lock_connection`), so a rule cannot interleave with a sync of its source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.db.models import STATUS_RULE_STATES, Connection, Deadline, Source, StatusRule
from botai.domain import keys
from botai.sources.normalize import lock_connection
from botai.sources.states import task_state

StateSource = Literal["student", "rule", "source"]
USER_STATES: tuple[str, ...] = ("open", "submitted", "graded")
MAX_LABEL = 120

# (source_id or None, normalized label) -> state
RuleMap = Mapping[tuple[str | None, str], str]


def normalize_label(value: str | None) -> str:
    """The comparable form of a status text: casefolded, ё as е, whitespace collapsed."""
    return keys.text_key(value)[:MAX_LABEL]


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


def base_state(row: Deadline) -> str | None:
    """The state of a task without the student's rules: the source's, else its words read by the
    vocabulary; None when the row has neither (it is then left as it is)."""
    if row.source_state is not None:
        return row.source_state
    if row.state_text is not None:
        return task_state(row.state_text)
    return None


async def _matching(
    session: AsyncSession, user_id: str, *, source_id: str | None, label: str
) -> list[Deadline]:
    stmt = (
        select(Deadline)
        .where(
            Deadline.user_id == user_id,
            Deadline.source_id.is_not(None),
            Deadline.state_text.is_not(None),
        )
        .execution_options(populate_existing=True)  # what a sync committed meanwhile
    )
    if source_id is not None:
        stmt = stmt.where(Deadline.source_id == source_id)
    return [row for row in await session.scalars(stmt) if normalize_label(row.state_text) == label]


async def _recompute(
    session: AsyncSession, user_id: str, *, source_id: str | None, label: str
) -> int:
    """Re-derive `state` of the user's v4 deadlines whose status text is `label` (within one
    source, or every source when `source_id` is None) from the rules that remain and the source's
    own state. The sources are locked first (in a stable order, like a sync locks one), then the
    rows are read again under the lock. `changed_at` is not touched. Returns how many changed."""
    if source_id is not None:
        sources = [source_id]
    else:
        found = await _matching(session, user_id, source_id=None, label=label)
        sources = sorted({row.source_id for row in found if row.source_id})
    if not sources:
        return 0
    for locked in sources:
        await lock_connection(session, locked)
    rows = await _matching(session, user_id, source_id=source_id, label=label)
    rules = await rules_of(session, user_id, source_ids=sources)
    changed = 0
    for row in rows:
        state = rule_state(rules, row.source_id, row.state_text) or base_state(row)
        if state is not None and row.state != state:
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
    """Create or replace the rule for (source, label) and recompute the stored tasks at once.

    One statement, `INSERT … ON CONFLICT DO UPDATE` on the partial unique index of its kind (a
    source's rule, or a rule for every source), so two identical requests at once both succeed."""
    if state not in STATUS_RULE_STATES:
        raise _fail("Состояние правила: open, submitted, graded или missed")
    normalized = normalize_label(label)
    if not normalized:
        raise _fail("Нужна подпись статуса из источника")
    if source_id is not None:
        await _owned_source(session, user_id, source_id)
    stmt = pg_insert(StatusRule).values(
        id=new_id(), user_id=user_id, source_id=source_id, label=normalized, state=state
    )
    if source_id is None:
        stmt = stmt.on_conflict_do_update(
            index_elements=[StatusRule.user_id, StatusRule.label],
            index_where=text("source_id IS NULL"),
            set_={"state": state},
        )
    else:
        stmt = stmt.on_conflict_do_update(
            index_elements=[StatusRule.user_id, StatusRule.source_id, StatusRule.label],
            index_where=text("source_id IS NOT NULL"),
            set_={"state": state},
        )
    rule_id = (await session.execute(stmt.returning(StatusRule.id))).scalar_one()
    rule = await session.get(StatusRule, rule_id, populate_existing=True)
    assert rule is not None  # just written in this transaction
    await _recompute(session, user_id, source_id=source_id, label=normalized)
    return rule


async def delete_status_rule(session: AsyncSession, user_id: str, rule_id: str) -> None:
    """Remove a rule; the tasks it decided fall back to another rule or to the source's state."""
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

_SPACE = func.coalesce(Source.space_id, Connection.space_id)


async def copies_of(session: AsyncSession, user_id: str, deadline_id: str) -> list[Deadline]:
    """The task and its copies in the user's other sources of the same university: the rows with
    the same dedupe key (space, title, due moment) as the reads use. The task itself first."""
    base = (
        select(Deadline, _SPACE)
        .outerjoin(Source, Source.id == Deadline.source_id)
        .outerjoin(Connection, Connection.id == Deadline.connection_id)
    )
    found = (
        await session.execute(base.where(Deadline.id == deadline_id, Deadline.user_id == user_id))
    ).first()
    if found is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Задание не найдено")
    target, space_id = found
    key = keys.deadline_key(target, space_id or "")
    due = func.coalesce(Deadline.effective_due_at, Deadline.due_at)
    rows = await session.execute(
        base.where(
            Deadline.user_id == user_id,
            Deadline.id != target.id,
            due.is_not_distinct_from(target.effective_due_at or target.due_at),
        )
    )
    return [target] + [
        row for row, space in rows.all() if keys.deadline_key(row, space or "") == key
    ]


async def set_user_state(
    session: AsyncSession, user_id: str, deadline_id: str, state: str | None
) -> Deadline:
    """Mark a task (`open` | `submitted` | `graded`) or clear the mark (None), on every copy of it.

    `changed_at` stays: it is the source's change, the student's own move is not news."""
    if state is not None and state not in USER_STATES:
        raise _fail("Отметка задания: open, submitted, graded или пусто")
    rows = await copies_of(session, user_id, deadline_id)
    for row in rows:
        row.user_state = state
    await session.flush()
    return rows[0]
