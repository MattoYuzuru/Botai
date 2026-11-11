"""What the chat agent knows about the student's studies before it calls a tool (D-044 §5.2).

Two things live here, both read-only over Postgres and both cheap:

* `source_freshness` — every live v4 source of the student with its status and the age of its
  last sync. `study_data` returns it with the records, and the digest prints it, so the model can
  judge whether the data are fresh enough or a `source_sync` is due;
* `study_digest` — a short per-university block of the turn context: the sources and their age,
  the nearest unfinished tasks with their states, the latest changes (new grades, status changes)
  and how many grades each layer has. A simple question («что у меня на этой неделе сдать?», «мне
  поставили за ДЗ 7?») is then answered without a single tool call.

The digest is data of the application, not the student's words. It is capped (`DIGEST_CHARS`),
ordered deterministically and goes after the stable system prefix. Its queries are few (spaces,
sources, deadlines, updates, one aggregate of grades); any failure skips the digest and logs the
exception class only — the turn never fails for it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import Grade, Source, Space
from botai.domain import repo
from botai.domain.sources import HIDDEN_V4

log = logging.getLogger(__name__)

DEFAULT_TZ = "Europe/Moscow"
DIGEST_CHARS = 1800
DIGEST_DEADLINES = 6
DIGEST_UPDATES = 6
UPDATES_DAYS = 7
OVERDUE_DAYS = 2  # a task overdue longer than this leaves «ближайшие сроки» (study_data has it)
MAX_DIGEST_SPACES = 6
MAX_DIGEST_SOURCES = 8
TITLE_CHARS = 70
DONE_STATES = frozenset({"submitted", "graded"})
LAYER_ORDER = ("item", "current", "final", "summary", "")
CUT_LINE = "  …сводка сокращена: подробности — study_data."


def zone_of(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo(DEFAULT_TZ)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def clip(text: Any, limit: int = TITLE_CHARS) -> str:
    """One line of data text: whitespace collapsed, cut with an ellipsis."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def age_text(minutes: int | None) -> str:
    """«обновлено 12 мин назад» in a few honest buckets; `None` — never synced."""
    if minutes is None:
        return "ни разу не обновлялся"
    if minutes < 1:
        return "обновлено только что"
    if minutes < 60:
        return f"обновлено {minutes} мин назад"
    hours = minutes // 60
    if hours < 48:
        return f"обновлено {hours} ч назад"
    return f"обновлено {hours // 24} дн назад"


# --- sources and their freshness ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpaceRef:
    id: str
    title: str
    zone: ZoneInfo


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    id: str
    title: str
    space_id: str
    space_title: str
    kinds: tuple[str, ...]
    status: str
    last_sync_at: datetime | None
    age_minutes: int | None

    def data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": clip(self.title, 120),
            "space": clip(self.space_title, 120),
            "spaceId": self.space_id,
            "kinds": list(self.kinds),
            "status": self.status,
            "lastSyncAt": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "ageMinutes": self.age_minutes,
        }


async def spaces_of(session: AsyncSession, user_id: str) -> list[SpaceRef]:
    """The student's universities, oldest first (one query)."""
    rows = (
        await session.execute(
            select(Space.id, Space.title, Space.time_zone)
            .where(Space.user_id == user_id)
            .order_by(Space.created_at, Space.id)
        )
    ).all()
    return [SpaceRef(id=sid, title=title, zone=zone_of(tz)) for sid, title, tz in rows]


async def source_freshness(
    session: AsyncSession,
    user_id: str,
    *,
    now: datetime,
    space_id: str | None = None,
    spaces: Iterable[SpaceRef] | None = None,
) -> list[SourceFreshness]:
    """Live v4 sources (not switched off) with their age, by university then creation (one query)."""
    titles = {space.id: space.title for space in spaces} if spaces is not None else None
    stmt = (
        select(Source, Space.title)
        .join(Space, Space.id == Source.space_id)
        .where(Source.user_id == user_id, Source.status.not_in(sorted(HIDDEN_V4)))
        .order_by(Space.created_at, Space.id, Source.created_at, Source.id)
    )
    if space_id is not None:
        stmt = stmt.where(Source.space_id == space_id)
    out: list[SourceFreshness] = []
    for source, space_title in (await session.execute(stmt)).all():
        synced = aware(source.last_sync_at) if source.last_sync_at is not None else None
        age = max(0, int((aware(now) - synced).total_seconds() // 60)) if synced else None
        out.append(
            SourceFreshness(
                id=source.id,
                title=source.title,
                space_id=source.space_id,
                space_title=(titles or {}).get(source.space_id, space_title),
                kinds=tuple(kind for kind in source.kinds if kind),
                status=source.status,
                last_sync_at=synced,
                age_minutes=age,
            )
        )
    return out


# --- the digest ---------------------------------------------------------------------------------


def dumped(item: Any) -> dict[str, Any]:
    """A domain read model as its camelCase API dict (a plain dict passes through)."""
    if isinstance(item, BaseModel):
        return item.model_dump(by_alias=True, mode="json", exclude_none=True)
    if isinstance(item, Mapping):
        return {str(key): value for key, value in item.items()}  # pyright: ignore[reportUnknownVariableType]
    return {}


def space_of(item: Mapping[str, Any]) -> str | None:
    provenance = item.get("provenance")
    if isinstance(provenance, Mapping):
        found = provenance.get("spaceId")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(found, str):
            return found
    found = item.get("spaceId")
    return found if isinstance(found, str) else None


def local_moment(value: Any, zone: ZoneInfo) -> str:
    """`12.11 23:59` in the university's zone; an unparsable value as is."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return clip(value, 25)
    return aware(moment).astimezone(zone).strftime("%d.%m %H:%M")


def _deadline_line(item: Mapping[str, Any], zone: ZoneInfo) -> str:
    due = local_moment(item.get("effectiveDueAt") or item.get("dueAt"), zone)
    course = item.get("courseTitle")
    state = str(item.get("state") or "unknown")
    words = item.get("stateText")
    head = f"«{clip(item.get('title'))}»" + (f" ({clip(course, 40)})" if course else "")
    status = state + (f" «{clip(words, 40)}»" if words else "")
    return f"{head}" + (f" до {due}" if due else "") + f" — {status}"


def _grade_value(item: Mapping[str, Any]) -> str:
    value = clip(item.get("value"), 20)
    top = item.get("scaleMax") or item.get("max")
    scale = item.get("scale")
    if isinstance(top, int | float) and not isinstance(top, bool):
        return f"{value}/{top:g}"
    return value + (f" ({clip(scale, 25)})" if scale else "")


def update_line(item: Mapping[str, Any], zone: ZoneInfo) -> str:
    """One recent change: a new/changed grade or a task whose state changed."""
    when = local_moment(item.get("changedAt"), zone)
    course = item.get("courseTitle")
    title = f"«{clip(item.get('title'), 60)}»" + (f" ({clip(course, 40)})" if course else "")
    kind = str(item.get("kind") or item.get("type") or "")
    if kind.startswith("grade") or ("value" in item and "state" not in item):
        text = f"оценка {title}: {_grade_value(item)}"
    else:
        state = str(item.get("state") or "?")
        words = item.get("stateText")
        text = f"статус {title}: {state}" + (f" «{clip(words, 40)}»" if words else "")
    return text + (f", {when}" if when else "")


def _due_soon(item: Mapping[str, Any], now: datetime) -> bool:
    """A dated task due from `OVERDUE_DAYS` ago on: long-overdue rows are noise in a digest."""
    raw = item.get("effectiveDueAt") or item.get("dueAt")
    if not isinstance(raw, str):
        return False
    try:
        due = aware(datetime.fromisoformat(raw))
    except ValueError:
        return False
    return due >= now - timedelta(days=OVERDUE_DAYS)


async def _grade_layers(session: AsyncSession, user_id: str) -> dict[str, dict[str, int]]:
    """Grades per university and layer, over the live v4 sources (one aggregate query)."""
    rows = (
        await session.execute(
            select(Source.space_id, Grade.layer, func.count())
            .join(Source, Source.id == Grade.source_id)
            .where(
                Grade.user_id == user_id,
                Source.user_id == user_id,
                Source.status.not_in(sorted(HIDDEN_V4)),
            )
            .group_by(Source.space_id, Grade.layer)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for space_id, layer, count in rows:
        bucket = out.setdefault(space_id, {})
        key = layer or ""
        bucket[key] = bucket.get(key, 0) + int(count)
    return out


def _layers_line(counts: Mapping[str, int]) -> str:
    parts = [f"{layer or 'без слоя'} {counts[layer]}" for layer in LAYER_ORDER if counts.get(layer)]
    parts.extend(
        f"{layer} {count}" for layer, count in sorted(counts.items()) if layer not in LAYER_ORDER
    )
    return ", ".join(parts)


async def study_digest(
    session: AsyncSession,
    user_id: str,
    *,
    now: datetime,
    space_id: str | None = None,
    limit: int = DIGEST_CHARS,
) -> str:
    """The per-university digest of the turn context; empty when the student has no university.

    `space_id` is the chat's university: it goes first. Everything is the application's data;
    nothing here comes from page text the student did not see in the app.
    """
    now = aware(now)
    spaces = await spaces_of(session, user_id)
    if not spaces:
        return ""
    spaces.sort(key=lambda space: space.id != space_id)  # stable: the chat's university first
    spaces = spaces[:MAX_DIGEST_SPACES]
    freshness = await source_freshness(session, user_id, now=now, spaces=spaces)
    deadlines = [
        dumped(item)
        for item in await repo.list_deadlines(session, user_id, include_done=False, now=now)
    ]
    updates = [
        dumped(item)
        for item in await repo.list_updates(
            session,
            user_id,
            since=now - timedelta(days=UPDATES_DAYS),
            limit=DIGEST_UPDATES * len(spaces),
        )
    ]
    layers = await _grade_layers(session, user_id)

    head = (
        "Сводка учёбы (данные приложения, не слова студента; полные записи, фильтры и ссылки — "
        f"study_data). Сейчас {now.astimezone(spaces[0].zone).strftime('%d.%m %H:%M')}."
    )
    essential: dict[str, list[str]] = {}
    details: dict[str, list[str]] = {}
    for space in spaces:
        mine = [source for source in freshness if source.space_id == space.id]
        lines = [
            f"- «{clip(space.title)}» space_id={space.id}"
            + (" — к нему относится этот чат" if space.id == space_id else "")
            + ("" if mine else ": источников пока нет")
        ]
        for source in mine[:MAX_DIGEST_SOURCES]:
            kinds = ", ".join(source.kinds) or "вид не указан"
            lines.append(
                f"  источник «{clip(source.title, 60)}» ({kinds}) — {source.status}, "
                f"{age_text(source.age_minutes)}; source_id={source.id}"
            )
        if len(mine) > MAX_DIGEST_SOURCES:
            lines.append("  …остальные источники: source_list.")
        essential[space.id] = lines

        extra: list[str] = []
        soon = [
            item
            for item in deadlines
            if space_of(item) == space.id
            and str(item.get("state")) not in DONE_STATES
            and _due_soon(item, now)
        ][:DIGEST_DEADLINES]
        if soon:
            extra.append(
                "  ближайшие сроки: " + "; ".join(_deadline_line(i, space.zone) for i in soon)
            )
        recent = [item for item in updates if space_of(item) == space.id][:DIGEST_UPDATES]
        if recent:
            extra.append(
                f"  новое за {UPDATES_DAYS} дн: "
                + "; ".join(update_line(i, space.zone) for i in recent)
            )
        counts = layers.get(space.id)
        if counts:
            extra.append("  оценок по слоям: " + _layers_line(counts))
        details[space.id] = extra

    budget = limit - len(head) - len(CUT_LINE) - 2  # room for the «сокращена» line
    kept: dict[str, list[str]] = {}
    for space in spaces:  # sources first: the ids are what every tool takes
        for line in essential[space.id]:
            if len(line) + 1 > budget:
                break
            kept.setdefault(space.id, []).append(line)
            budget -= len(line) + 1
    cut = False
    for space in spaces:
        for line in details[space.id]:
            if len(line) + 1 > budget:
                cut = True
                continue
            kept.setdefault(space.id, []).append(line)
            budget -= len(line) + 1
    out = [head]
    for space in spaces:
        out.extend(kept.get(space.id, []))
    if cut:
        out.append(CUT_LINE)
    return "\n".join(out)


async def safe_study_digest(
    session: AsyncSession, user_id: str, *, now: datetime, space_id: str | None = None
) -> str:
    """`study_digest`, or an empty string when any read fails (the type of the error is logged).

    The digest runs inside a savepoint so a failed query does not poison the turn's session.
    """
    try:
        async with session.begin_nested():
            return await study_digest(session, user_id, now=now, space_id=space_id)
    except Exception as exc:  # the text may carry data: the class only
        log.warning("study digest skipped: %s", type(exc).__name__)
        return ""


__all__ = [
    "DIGEST_CHARS",
    "SourceFreshness",
    "SpaceRef",
    "age_text",
    "clip",
    "dumped",
    "safe_study_digest",
    "source_freshness",
    "spaces_of",
    "study_digest",
]
