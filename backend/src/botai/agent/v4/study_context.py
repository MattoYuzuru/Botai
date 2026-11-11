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
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.agent.env import data_text
from botai.db.models import Source, Space
from botai.domain import repo
from botai.domain.sources import HIDDEN_V4

log = logging.getLogger(__name__)

DEFAULT_TZ = "Europe/Moscow"
DIGEST_CHARS = 4500
DIGEST_OPEN = 12  # not-done tasks, nearest first
DIGEST_DONE = 5  # recently done tasks
DIGEST_GRADES = 20  # latest grades
DIGEST_UPDATES = 6
UPDATES_DAYS = 7
OVERDUE_DAYS = 14  # a task overdue longer than this leaves the digest (study_data has it)
MAX_DIGEST_SPACES = 6
MAX_DIGEST_SOURCES = 8
TITLE_CHARS = 60
COURSE_CHARS = 24
DONE_STATES = frozenset({"submitted", "graded"})
LAYER_ORDER = ("item", "current", "final", "summary", "")
EVENT_KINDS_RU = {
    "lecture": "лекция",
    "seminar": "семинар",
    "lab": "лабораторная",
    "exam": "экзамен",
    "consultation": "консультация",
}
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
HEAD_RULE = "Если ответ есть в сводке — отвечай сразу, без инструментов; за остальным — study_data."
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


def safe(text: Any, limit: int = TITLE_CHARS) -> str:
    """Page-derived text inside the digest: one line, cut, wrapper look-alikes and
    instruction-like sentences neutralised (`env.data_text`, as in the turn context)."""
    return data_text(" ".join(str(text or "").split()), limit)


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
        return safe(value, 25)
    return aware(moment).astimezone(zone).strftime("%d.%m %H:%M")


def _short_course(item: Mapping[str, Any]) -> str:
    course = item.get("courseTitle")
    return f" ({safe(course, COURSE_CHARS)})" if course else ""


def grade_value(value: Any, scale: Any, top: Any) -> str:
    """«8 из 10» when the source gave the top of the scale as a number, else its own text."""
    text = safe(value, 20)
    if isinstance(top, int | float) and not isinstance(top, bool) and top:
        return f"{text} из {top:g}"
    return text + (f" ({safe(scale, 25)})" if scale else "")


def _deadline_line(item: Mapping[str, Any], zone: ZoneInfo) -> str:
    due = local_moment(item.get("effectiveDueAt") or item.get("dueAt"), zone)
    state = str(item.get("state") or "unknown")
    words = item.get("stateText")
    line = f"«{safe(item.get('title'))}»{_short_course(item)}" + (f" до {due}" if due else "")
    line += f" — {state}" + (f" «{safe(words, 40)}»" if words else "")
    grade = item.get("grade")
    if isinstance(grade, Mapping):
        line += ", балл " + grade_value(
            grade.get("value"),  # pyright: ignore[reportUnknownMemberType]
            grade.get("scale"),  # pyright: ignore[reportUnknownMemberType]
            grade.get("scaleMax"),  # pyright: ignore[reportUnknownMemberType]
        )
    return line


def _grade_line(item: Mapping[str, Any], zone: ZoneInfo) -> str:
    course = item.get("courseTitle")
    head = f"{safe(course, COURSE_CHARS)} — " if course else ""
    value = grade_value(item.get("value"), item.get("scale"), item.get("scaleMax"))
    line = f"{head}«{safe(item.get('title') or 'оценка')}»: {value}"
    layer = item.get("layer")
    if layer and layer != "item":
        line += f", {layer}"
    when = local_moment(item.get("gradedAt"), zone)
    return line + (f", {when[:5]}" if when else "")


def _event_line(item: Mapping[str, Any], zone: ZoneInfo) -> str:
    start = local_moment(item.get("startsAt"), zone)[-5:]
    end = local_moment(item.get("endsAt"), zone)[-5:]
    kind = item.get("kindText") or EVENT_KINDS_RU.get(str(item.get("kind")))
    line = f"{start}–{end} «{safe(item.get('title'))}»"
    if kind:
        line += f" {safe(kind, 20)}"
    place = item.get("location") or item.get("address")
    if place:
        line += f", {safe(place, 30)}"
    if item.get("format") == "online":
        line += ", онлайн"
    return line


def update_line(item: Mapping[str, Any], zone: ZoneInfo) -> str:
    """One recent change: a new/changed grade or a task whose state changed."""
    when = local_moment(item.get("changedAt"), zone)
    title = f"«{safe(item.get('title'))}»{_short_course(item)}"
    kind = str(item.get("kind") or item.get("type") or "")
    if kind.startswith("grade") or ("value" in item and "state" not in item):
        value = grade_value(item.get("value"), item.get("scale"), item.get("scaleMax"))
        text = f"оценка {title}: {value}"
    else:
        state = str(item.get("state") or "?")
        words = item.get("stateText")
        text = f"статус {title}: {state}" + (f" «{safe(words, 40)}»" if words else "")
    return text + (f", {when}" if when else "")


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def _due(item: Mapping[str, Any]) -> datetime | None:
    return _moment(item.get("effectiveDueAt") or item.get("dueAt"))


def _graded(item: Mapping[str, Any]) -> datetime:
    provenance = item.get("provenance")
    fetched = (
        provenance.get("fetchedAt")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(provenance, Mapping)
        else None
    )
    for value in (item.get("gradedAt"), item.get("changedAt"), fetched):
        moment = _moment(value)
        if moment is not None:
            return moment
    return datetime.min.replace(tzinfo=UTC)


def _open_tasks(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Not-done tasks, nearest first (recently overdue included), undated ones last."""
    oldest = now - timedelta(days=OVERDUE_DAYS)
    dated = [
        (due, str(i.get("id")), i)
        for i in items
        if str(i.get("state")) not in DONE_STATES and (due := _due(i)) is not None and due >= oldest
    ]
    undated = [i for i in items if str(i.get("state")) not in DONE_STATES and _due(i) is None]
    return [i for _, _, i in sorted(dated, key=lambda t: (t[0], t[1]))] + undated


def _done_tasks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    done = [i for i in items if str(i.get("state")) in DONE_STATES]
    floor = datetime.min.replace(tzinfo=UTC)
    return sorted(
        done,
        key=lambda i: (_moment(i.get("changedAt")) or _due(i) or floor, str(i.get("id"))),
        reverse=True,
    )


def _layers_line(grades: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for item in grades:
        key = str(item.get("layer") or "")
        counts[key] = counts.get(key, 0) + 1
    parts = [f"{layer or 'без слоя'} {counts[layer]}" for layer in LAYER_ORDER if counts.get(layer)]
    parts.extend(
        f"{layer} {count}" for layer, count in sorted(counts.items()) if layer not in LAYER_ORDER
    )
    return ", ".join(parts)


def _day(moment: datetime) -> str:
    return f"{WEEKDAYS_SHORT[moment.weekday()]} {moment.strftime('%d.%m')}"


# Sections of one university, in display order; `FILL_ORDER` is who gets the budget first.
SECTIONS = ("open", "today", "tomorrow", "grades", "layers", "done", "updates")
FILL_ORDER = ("open", "today", "tomorrow", "grades", "updates", "done", "layers")
ITEM = "   · "


async def _space_sections(
    session: AsyncSession,
    user_id: str,
    space: SpaceRef,
    *,
    now: datetime,
    deadlines: list[dict[str, Any]],
    grades: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Header + item lines of each section of one university (empty sections are absent)."""
    zone = space.zone
    out: dict[str, list[str]] = {}
    mine = [i for i in deadlines if space_of(i) == space.id]
    open_tasks = _open_tasks(mine, now)[:DIGEST_OPEN]
    if open_tasks:
        out["open"] = ["  не сдано (ближайшие):"] + [
            ITEM + _deadline_line(i, zone) for i in open_tasks
        ]
    done = _done_tasks(mine)[:DIGEST_DONE]
    if done:
        out["done"] = ["  сдано недавно:"] + [ITEM + _deadline_line(i, zone) for i in done]
    today = datetime.combine(now.astimezone(zone).date(), time(), tzinfo=zone)
    events = [
        dumped(e)
        for e in await repo.list_events(
            session, user_id, start=today, end=today + timedelta(days=2), space_id=space.id, now=now
        )
    ]
    for name, day in (("today", today), ("tomorrow", today + timedelta(days=1))):
        that_day = [
            e
            for e in events
            if (start := _moment(e.get("startsAt"))) is not None
            and day <= start.astimezone(zone) < day + timedelta(days=1)
        ]
        label = "сегодня" if name == "today" else "завтра"
        if that_day:
            out[name] = [f"  пары {label} ({_day(day)}):"] + [
                ITEM + _event_line(e, zone) for e in that_day
            ]
        else:
            out[name] = [f"  пары {label} ({_day(day)}): нет"]
    own_grades = [g for g in grades if space_of(g) == space.id]
    latest = sorted(own_grades, key=lambda g: (_graded(g), str(g.get("id"))), reverse=True)
    if latest:
        out["grades"] = ["  оценки (последние):"] + [
            ITEM + _grade_line(g, zone) for g in latest[:DIGEST_GRADES]
        ]
        out["layers"] = ["  оценок по слоям: " + _layers_line(own_grades)]
    recent = [i for i in updates if space_of(i) == space.id][:DIGEST_UPDATES]
    if recent:
        out["updates"] = [f"  новое за {UPDATES_DAYS} дн:"] + [
            ITEM + update_line(i, zone) for i in recent
        ]
    return out


async def study_digest(
    session: AsyncSession,
    user_id: str,
    *,
    now: datetime,
    space_id: str | None = None,
    limit: int = DIGEST_CHARS,
) -> str:
    """The per-university digest of the turn context; empty when the student has no university.

    `space_id` is the chat's university: it goes first. Sources (with their ids) always come
    first; then, while the budget lasts, the sections in `FILL_ORDER`, each university in turn.
    A section whose first item does not fit is left out and the digest says it was cut.
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
        for item in await repo.list_deadlines(session, user_id, include_done=True, now=now)
    ]
    grades = [dumped(item) for item in await repo.list_grades(session, user_id, now=now)]
    updates = [
        dumped(item)
        for item in await repo.list_updates(
            session,
            user_id,
            since=now - timedelta(days=UPDATES_DAYS),
            limit=DIGEST_UPDATES * len(spaces),
        )
    ]
    head = (
        "Сводка учёбы (данные приложения, не слова студента; полные записи, фильтры и ссылки — "
        f"study_data). Сейчас {now.astimezone(spaces[0].zone).strftime('%d.%m %H:%M')}.\n"
        + HEAD_RULE
    )
    essential: dict[str, list[str]] = {}
    sections: dict[str, dict[str, list[str]]] = {}
    for space in spaces:
        mine = [source for source in freshness if source.space_id == space.id]
        lines = [
            f"- «{safe(space.title)}» space_id={space.id}"
            + (" — к нему относится этот чат" if space.id == space_id else "")
            + ("" if mine else ": источников пока нет")
        ]
        for source in mine[:MAX_DIGEST_SOURCES]:
            kinds = ", ".join(source.kinds) or "вид не указан"
            lines.append(
                f"  источник «{safe(source.title, 60)}» ({kinds}) — {source.status}, "
                f"{age_text(source.age_minutes)}; source_id={source.id}"
            )
        if len(mine) > MAX_DIGEST_SOURCES:
            lines.append("  …остальные источники: source_list.")
        essential[space.id] = lines
        sections[space.id] = await _space_sections(
            session,
            user_id,
            space,
            now=now,
            deadlines=deadlines,
            grades=grades,
            updates=updates,
        )

    budget = limit - len(head) - len(CUT_LINE) - 2  # room for the «сокращена» line
    kept: dict[str, list[str]] = {space.id: [] for space in spaces}
    for space in spaces:  # sources first: the ids are what every tool takes
        for line in essential[space.id]:
            if len(line) + 1 > budget:
                break
            kept[space.id].append(line)
            budget -= len(line) + 1
    chosen: dict[str, dict[str, list[str]]] = {space.id: {} for space in spaces}
    cut = False
    for name in FILL_ORDER:
        for space in spaces:
            lines = sections[space.id].get(name)
            if not lines:
                continue
            first = len(lines[0]) + 1 + (len(lines[1]) + 1 if len(lines) > 1 else 0)
            if first > budget:
                cut = True
                continue
            taken = lines[:2] if len(lines) > 1 else lines[:1]
            budget -= first
            for line in lines[2:]:
                if len(line) + 1 > budget:
                    cut = True
                    break
                taken.append(line)
                budget -= len(line) + 1
            chosen[space.id][name] = taken
    out = [head]
    for space in spaces:
        out.extend(kept[space.id])
        for name in SECTIONS:
            out.extend(chosen[space.id].get(name, []))
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
