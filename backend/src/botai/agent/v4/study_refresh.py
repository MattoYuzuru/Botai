"""`study_data(refresh=true)`: bring the needed sources up to date first, without the model.

The live run of 2026-11-12 (DeepSeek, a copy of production data): a freshness question («уже
поставили?») cost three model steps (study_data → source_sync → study_data, ~6 s each), and a
failed sync sent the agent on a 20-step repair spree instead of answering from the saved data.
So the refresh is part of the one read the agent makes:

* the candidates are the student's live sources (narrowed by `space_id`) that provide one of the
  requested kinds — by the kinds they declare, or by the rows they already hold;
* a source synced less than `max_age_minutes` ago is `fresh`; one without a playbook, or waiting
  for the student (`needs_reauth`, `needs_link`, `waiting_user`), is `skipped` and never tried;
* the rest run the deterministic sync of `source_sync` (`SourceToolkit.sync_source`), a few at a
  time, each in its own session, under one overall cap; a source still running at the cap is
  cancelled and reported `timeout`, its saved data stay what the read returns;
* each outcome is data for the model: `refresh`, a short `refreshError` code with a plain-words
  `refreshReason`, the domain `changes` and the `warnings` codes of the sync.

Nothing here raises for one source: a failure is that source's outcome, the read goes on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.v4.study_context import aware
from botai.core.errors import BotaiError
from botai.db.models import (
    Course,
    Deadline,
    Grade,
    ProfileField,
    RankingEntry,
    ScheduleEvent,
    Source,
)
from botai.domain.sources import HIDDEN_V4

log = logging.getLogger(__name__)

RefreshStatus = Literal["ok", "fresh", "failed", "timeout", "skipped"]
# (user_id, source_id) -> the run report of a deterministic sync (`SourceToolkit.sync_source`).
Syncer = Callable[[str, str], Awaitable[Any]]

REFRESH_CAP_S = 25.0
REFRESH_CONCURRENCY = 3
UNWIND_S = 3.0
DEFAULT_MAX_AGE_MINUTES = 15
# A source in one of these states needs the student, not a retry: it is not attempted.
WAITING_STATUSES = frozenset({"needs_reauth", "needs_link", "waiting_user"})

# The source data kinds and the domain table behind each study kind.
SOURCE_KINDS: dict[str, frozenset[str]] = {
    "grades": frozenset({"grades"}),
    "deadlines": frozenset({"deadlines", "assignments"}),
    "schedule": frozenset({"schedule"}),
    "courses": frozenset({"courses"}),
    "profile": frozenset({"profile"}),
    "ranking": frozenset({"ranking"}),
    "updates": frozenset({"grades", "deadlines", "assignments"}),
}
TABLES: dict[str, tuple[Any, ...]] = {
    "grades": (Grade,),
    "deadlines": (Deadline,),
    "schedule": (ScheduleEvent,),
    "courses": (Course,),
    "profile": (ProfileField,),
    "ranking": (RankingEntry,),
    "updates": (Grade, Deadline),
}
DATA_KINDS = frozenset().union(*SOURCE_KINDS.values()) | {
    "people",
    "locations",
    "news",
    "documents",
    "messages",
}
# What the student is told, in plain words, for a code of a skipped or failed refresh.
REASONS: dict[str, str] = {
    "needs_reauth": "нужно войти в кабинет заново",
    "source_auth_required": "нужно войти в кабинет заново",
    "needs_link": "нужна новая ссылка на календарь",
    "waiting_user": "источник ждёт ответа студента",
    "no_playbook": "у источника ещё нет обработчика",
    "timeout": "сайт не ответил вовремя",
    "source_unavailable": "сайт недоступен",
    "recipe_drift": "сайт изменился, обработчик нужно поправить",
    "recipe_rejected": "обработчик не смог разобрать ответ сайта",
    "unavailable": "обновление в этом ходе недоступно",
}
DEFAULT_REASON = "обновление не удалось"


@dataclass(slots=True)
class RefreshOutcome:
    status: RefreshStatus
    error: str | None = None
    changes: dict[str, dict[str, int]] = field(default_factory=lambda: {})
    warnings: list[str] = field(default_factory=lambda: [])

    def data(self) -> dict[str, Any]:
        out: dict[str, Any] = {"refresh": self.status}
        if self.error:
            out["refreshError"] = self.error
            out["refreshReason"] = REASONS.get(self.error, DEFAULT_REASON)
        if self.changes:
            out["changes"] = self.changes
        if self.warnings:
            out["warnings"] = self.warnings
        return out


def _wanted_source_kinds(kinds: Iterable[str]) -> frozenset[str]:
    return frozenset().union(*(SOURCE_KINDS.get(kind, frozenset()) for kind in kinds))


async def _holders(
    session: AsyncSession, user_id: str, kinds: Iterable[str], candidates: list[str]
) -> set[str]:
    """Sources that already hold rows of the requested kinds (one small query per table)."""
    if not candidates:
        return set()
    models = {model for kind in kinds for model in TABLES.get(kind, ())}
    found: set[str] = set()
    for model in sorted(models, key=lambda m: m.__tablename__):
        rows = await session.scalars(
            select(model.source_id)
            .where(model.user_id == user_id, model.source_id.in_(candidates))
            .distinct()
        )
        found.update(row for row in rows if row)
    return found


async def plan_refresh(
    session: AsyncSession,
    user_id: str,
    *,
    kinds: Iterable[str],
    space_id: str | None,
    now: datetime,
    max_age_minutes: int,
) -> tuple[dict[str, RefreshOutcome], list[str]]:
    """(decided outcomes, sources to sync): fresh and skipped are decided here, the rest is due."""
    kinds = list(kinds)
    stmt = select(Source).where(Source.user_id == user_id, Source.status.not_in(sorted(HIDDEN_V4)))
    if space_id is not None:
        stmt = stmt.where(Source.space_id == space_id)
    sources = list(await session.scalars(stmt.order_by(Source.created_at, Source.id)))
    wanted = _wanted_source_kinds(kinds)
    unlabelled = [s.id for s in sources if not (set(s.kinds) & DATA_KINDS)]
    holders = await _holders(
        session, user_id, kinds, [s.id for s in sources if s.id not in unlabelled]
    )
    decided: dict[str, RefreshOutcome] = {}
    due: list[str] = []
    limit = aware(now) - timedelta(minutes=max_age_minutes)
    for source in sources:
        relevant = bool(set(source.kinds) & wanted) or source.id in unlabelled
        if not relevant and source.id not in holders:
            continue
        if source.status in WAITING_STATUSES:
            decided[source.id] = RefreshOutcome("skipped", error=source.status)
        elif source.playbook_id is None:
            decided[source.id] = RefreshOutcome("skipped", error="no_playbook")
        elif (
            max_age_minutes > 0
            and source.last_sync_at is not None
            and aware(source.last_sync_at) > limit
        ):
            decided[source.id] = RefreshOutcome("fresh")
        else:
            due.append(source.id)
    return decided, due


def outcome_of(report: Any) -> RefreshOutcome:
    """A sync report as a refresh outcome: what changed, the warnings codes, the first error."""
    changes: dict[str, dict[str, int]] = {}
    domain: Mapping[str, Mapping[str, int]] = getattr(report, "domain", None) or {}
    for kind, counts in domain.items():
        kept = {
            name: int(counts.get(name, 0))
            for name in ("inserted", "updated")
            if counts.get(name, 0)
        }
        if kept:
            changes[kind] = kept
    warnings: list[str] = []
    for item in getattr(report, "warnings", None) or []:
        code = getattr(item, "code", None) or (item.get("code") if isinstance(item, dict) else None)  # pyright: ignore[reportUnknownMemberType]
        if isinstance(code, str) and code not in warnings:
            warnings.append(code)
    if getattr(report, "ok", False):
        return RefreshOutcome("ok", changes=changes, warnings=warnings)
    errors = list(getattr(report, "errors", None) or [])
    status = getattr(report, "source_status", None)
    code = (
        status
        if status in WAITING_STATUSES
        else (getattr(errors[0], "code", None) if errors else None) or "sync_failed"
    )
    return RefreshOutcome("failed", error=str(code)[:60], changes=changes, warnings=warnings)


async def run_refresh(
    sync: Syncer,
    user_id: str,
    source_ids: list[str],
    *,
    cap_s: float = REFRESH_CAP_S,
    concurrency: int = REFRESH_CONCURRENCY,
) -> dict[str, RefreshOutcome]:
    """Sync the due sources, a few at a time, under one overall cap; never raises."""
    if not source_ids:
        return {}
    gate = asyncio.Semaphore(concurrency)

    async def one(source_id: str) -> RefreshOutcome:
        async with gate:
            try:
                return outcome_of(await sync(user_id, source_id))
            except BotaiError as exc:
                return RefreshOutcome("failed", error=exc.code.value)
            except Exception as exc:  # the text may carry data: the class only
                log.warning("study refresh of a source failed: %s", type(exc).__name__)
                return RefreshOutcome("failed", error=type(exc).__name__[:60])

    tasks = {source_id: asyncio.create_task(one(source_id)) for source_id in source_ids}
    await asyncio.wait(tasks.values(), timeout=cap_s)
    out: dict[str, RefreshOutcome] = {}
    for source_id, task in tasks.items():
        if task.done() and not task.cancelled():
            out[source_id] = task.result()
        else:
            task.cancel()
            out[source_id] = RefreshOutcome("timeout", error="timeout")
    # Let the cancelled syncs unwind (their sessions roll back) before the read starts, but do
    # not wait for one that ignores the cancellation.
    stuck = [task for task in tasks.values() if not task.done()]
    if stuck:
        await asyncio.wait(stuck, timeout=UNWIND_S)
    return out


__all__ = [
    "DEFAULT_MAX_AGE_MINUTES",
    "REASONS",
    "REFRESH_CAP_S",
    "RefreshOutcome",
    "Syncer",
    "outcome_of",
    "plan_refresh",
    "run_refresh",
]
