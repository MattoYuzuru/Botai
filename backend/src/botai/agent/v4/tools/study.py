"""`study_data`, `study_status_rule`, `study_mark_task`: the student's synced data and corrections.

The production audit of 2026-11-11 (docs/v4/27-study-hub-grades-fastpath.md §2): a chat question
like «мне поставили оценку за ДЗ 7?» took 15–109 model steps and up to 20 minutes, because the
agent had no way to read what BotAI already holds and went to the university site every time.

* `study_data` reads the synced records from Postgres through `domain.repo` — the same tenant-scoped
  reads the screens use (live sources only, merged, deduplicated) — in one call, filtered and
  compacted for the model, together with the freshness of every source. Read-only, idempotent,
  no network, no model.
* `study_status_rule` and `study_mark_task` are the student's deterministic corrections (D-045)
  over `domain.overrides`: «в этом источнике „Решение прикреплено“ значит сдано» and «ДЗ 9 я
  сдал». They change the data at once; the result says how many tasks changed.

Every call is scoped to the run's user: another student's rows never come back, a foreign id is
`not_found`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.agent.v4.protocol import (
    FunctionTool,
    ToolContext,
    ToolHandler,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.agent.v4.study_context import (
    SpaceRef,
    aware,
    clip,
    dumped,
    source_freshness,
    space_of,
    spaces_of,
    zone_of,
)
from botai.core.clock import Clock, SystemClock
from botai.core.errors import BotaiError
from botai.db.models import STATUS_RULE_STATES, Deadline, StatusRule
from botai.domain import overrides, repo

StudyKind = Literal["grades", "deadlines", "schedule", "courses", "profile", "ranking", "updates"]
STUDY_KINDS: tuple[StudyKind, ...] = (
    "grades",
    "deadlines",
    "schedule",
    "courses",
    "profile",
    "ranking",
    "updates",
)
GRADE_LAYERS = ("item", "current", "final", "summary")
DEADLINE_STATES = ("open", "submitted", "graded", "missed", "unknown")
DONE_STATES = frozenset({"submitted", "graded"})
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
SCHEDULE_DAYS = 7
DONE_DAYS = 30
UPDATES_DAYS = 7
TEXT_CHARS = 160
COMMENT_CHARS = 300
MAX_RESULT_CHARS = 40_000

# The fields each kind keeps for the model (camelCase of the API read models), in this order.
# `url` is the entity's own page; the provenance `openUrl` is used when the row has none.
KEEP: dict[str, tuple[str, ...]] = {
    "grades": (
        "id",
        "courseTitle",
        "title",
        "value",
        "scale",
        "scaleMax",
        "valueNum",
        "weight",
        "layer",
        "period",
        "assignmentId",
        "gradedAt",
        "changedAt",
        "comment",
        "url",
    ),
    "deadlines": (
        "id",
        "courseTitle",
        "title",
        "dueAt",
        "effectiveDueAt",
        "state",
        "stateText",
        "stateSource",
        "grade",
        "changedAt",
        "url",
    ),
    "schedule": (
        "id",
        "title",
        "courseTitle",
        "kindText",
        "kind",
        "startsAt",
        "endsAt",
        "format",
        "location",
        "address",
        "teacher",
        "group",
        "changed",
        "url",
    ),
    "courses": ("id", "title", "code", "term", "url"),
    "profile": ("id", "group", "label", "value"),
    "ranking": ("id", "board", "period", "position", "total", "label", "score", "isMe"),
    "updates": (),  # whatever the update says, minus provenance noise (see `_compact`)
}
LONG_FIELDS = {"comment": COMMENT_CHARS}
NOISE = frozenset({"provenance", "sources", "conflictsWith", "attributes", "links", "description"})


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StudyDataArgs(_Args):
    kinds: list[StudyKind] = Field(min_length=1, max_length=len(STUDY_KINDS))
    space_id: str | None = Field(default=None, max_length=26)
    course: str | None = Field(default=None, max_length=200)
    query: str | None = Field(default=None, max_length=200)
    from_: str | None = Field(default=None, alias="from", max_length=40)
    to: str | None = Field(default=None, max_length=40)
    state: Literal["open", "submitted", "graded", "missed", "unknown"] | None = None
    layer: Literal["item", "current", "final", "summary"] | None = None
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("kinds")
    @classmethod
    def _unique(cls, value: list[StudyKind]) -> list[StudyKind]:
        return list(dict.fromkeys(value))


class StatusRuleArgs(_Args):
    action: Literal["set", "list", "delete"]
    source_id: str | None = Field(default=None, max_length=26)
    label: str | None = Field(default=None, max_length=120)
    state: Literal["open", "submitted", "graded", "missed"] | None = None
    rule_id: str | None = Field(default=None, max_length=26)


class MarkTaskArgs(_Args):
    deadline_id: str = Field(min_length=1, max_length=26)
    state: Literal["submitted", "open", "graded"] | None


def _invalid(exc: ValidationError) -> ToolResult:
    parts: list[str] = []
    for error in exc.errors()[:4]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
        parts.append(f"{location}: {error.get('msg', 'ошибка')}")
    return ToolResult.failure("invalid_arguments", "; ".join(parts))


# --- time window helpers ------------------------------------------------------------------------


def parse_moment(value: str | None, zone: ZoneInfo, *, end: bool = False) -> datetime | None:
    """An ISO date (`2026-11-12`) or datetime; a date is its local midnight (`end`: the next one).

    Raises `ValueError` for anything else: the model gets a structured error, not an empty list.
    """
    if value is None or not value.strip():
        return None
    text = value.strip()
    if len(text) == 10:
        day = date.fromisoformat(text)
        if end:
            day += timedelta(days=1)
        return datetime.combine(day, time(), tzinfo=zone)
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


def _moment_of(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def _within(value: Any, start: datetime | None, end: datetime | None) -> bool:
    if start is None and end is None:
        return True
    moment = _moment_of(value)
    if moment is None:
        return False
    return (start is None or moment >= start) and (end is None or moment < end)


def _contains(value: Any, needle: str | None) -> bool:
    if not needle:
        return True
    return _fold(needle) in _fold(value)


def _fold(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


# --- compaction ---------------------------------------------------------------------------------


def _short(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return clip(value, limit)
    if isinstance(value, Mapping):
        return {
            str(key): _short(item, limit)  # pyright: ignore[reportUnknownArgumentType]
            for key, item in value.items()  # pyright: ignore[reportUnknownVariableType]
            if item is not None
        }
    return value


def _compact(kind: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """Only the useful fields of one record, short texts, the entity link and its university."""
    keep = KEEP[kind]
    names = keep or tuple(name for name in item if name not in NOISE)
    out: dict[str, Any] = {}
    for name in names:
        value = item.get(name)
        if value is None or value == "" or value == []:
            continue
        out[name] = _short(value, LONG_FIELDS.get(name, TEXT_CHARS))
    provenance = item.get("provenance")
    if isinstance(provenance, Mapping):
        if "url" not in out:
            link = provenance.get("openUrl")  # pyright: ignore[reportUnknownMemberType]
            if isinstance(link, str) and link:
                out["url"] = link
        source_id = provenance.get("sourceId")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(source_id, str):
            out["sourceId"] = source_id
        if provenance.get("stale") is True:  # pyright: ignore[reportUnknownMemberType]
            out["stale"] = True
    space = space_of(item)
    if space is not None:
        out["spaceId"] = space
    return out


def _section(kind: str, rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    items = [_compact(kind, row) for row in rows[:limit]]
    section: dict[str, Any] = {"count": len(rows), "truncated": len(rows) > limit}
    # One source (or one university) for the whole section: said once, not on every record.
    for name in ("sourceId", "spaceId"):
        values = {item.get(name) for item in items}
        if len(values) == 1 and None not in values:
            section[name] = values.pop()
            for item in items:
                item.pop(name, None)
    section["items"] = items
    return section


class StudyToolkit:
    """The study tools over one session factory; `clock` decides «today» and freshness."""

    data_spec = ToolSpec(
        name="study_data",
        description=(
            "Данные студента, уже сохранённые в BotAI синхронизацией (без сайта, мгновенно): "
            "grades — оценки (слой item/current/final/summary, балл из максимума, период, "
            "комментарий, ссылка); deadlines — задания и сроки со статусом (state, слова "
            "источника stateText, кто решил stateSource, оценка); schedule — пары (по умолчанию "
            "сегодня + 7 дней); courses, profile, ranking; updates — что изменилось (по умолчанию "
            "7 дней). Фильтры: course и query — подстрока предмета и названия, from/to — ISO "
            "дата/время, state (задания), layer (оценки), limit на вид. В ответе sources: "
            "статус и возраст каждого источника (ageMinutes) — по ним решай, нужен ли "
            "source_sync. Задания по умолчанию: несданные + сданные за 30 дней."
        ),
        parameters=object_schema(
            {
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(STUDY_KINDS)},
                    "minItems": 1,
                    "maxItems": len(STUDY_KINDS),
                },
                "space_id": {"type": ["string", "null"], "description": "Вуз (space_id)"},
                "course": {"type": ["string", "null"], "description": "Подстрока предмета"},
                "query": {"type": ["string", "null"], "description": "Подстрока названия записи"},
                "from": {"type": ["string", "null"], "description": "С какой даты/времени (ISO)"},
                "to": {"type": ["string", "null"], "description": "До какой даты/времени (ISO)"},
                "state": {"type": ["string", "null"], "enum": [*DEADLINE_STATES, None]},
                "layer": {"type": ["string", "null"], "enum": [*GRADE_LAYERS, None]},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                    "default": DEFAULT_LIMIT,
                },
            },
            required=["kinds"],
        ),
        timeout_s=10.0,
        policy="read",
        idempotent=True,
        max_result_chars=MAX_RESULT_CHARS,
    )
    status_rule_spec = ToolSpec(
        name="study_status_rule",
        description=(
            "Правило статуса заданий по словам источника: «в этом источнике „Решение "
            "прикреплено“ значит submitted». set — создать/заменить (label — stateText как в "
            "данных, state — open/submitted/graded/missed, source_id — один источник или null "
            "для всех); сразу пересчитывает сохранённые задания и применяется при каждой "
            "синхронизации. list — правила студента; delete — убрать (rule_id). Используй, когда "
            "студент говорит, что статус показан неверно и это повторяющаяся формулировка."
        ),
        parameters=object_schema(
            {
                "action": {"type": "string", "enum": ["set", "list", "delete"]},
                "source_id": {"type": ["string", "null"]},
                "label": {"type": ["string", "null"], "maxLength": 120},
                "state": {"type": ["string", "null"], "enum": [*STATUS_RULE_STATES, None]},
                "rule_id": {"type": ["string", "null"]},
            },
            required=["action"],
        ),
        timeout_s=15.0,
        policy="write_soft",
        idempotent=True,
        serial_only=True,
    )
    mark_task_spec = ToolSpec(
        name="study_mark_task",
        description=(
            "Отметка студента на одном задании, сильнее источника и правил: submitted — «сдал», "
            "open — «вернуть в работу», graded — «проверено», null — снять отметку (снова решает "
            "источник). deadline_id — id из study_data. Используй по словам студента о "
            "конкретном задании."
        ),
        parameters=object_schema(
            {
                "deadline_id": {"type": "string", "minLength": 1, "maxLength": 26},
                "state": {
                    "type": ["string", "null"],
                    "enum": ["submitted", "open", "graded", None],
                },
            },
            required=["deadline_id", "state"],
        ),
        timeout_s=15.0,
        policy="write_soft",
        idempotent=True,
        serial_only=True,
    )

    def __init__(self, *, sm: async_sessionmaker[AsyncSession], clock: Clock | None = None) -> None:
        self._sm = sm
        self._clock = clock or SystemClock()
        self._handlers: list[ToolHandler] = [
            FunctionTool(self.data_spec, self._data),
            FunctionTool(self.status_rule_spec, self._status_rule),
            FunctionTool(self.mark_task_spec, self._mark_task),
        ]

    def tools(self) -> list[ToolHandler]:
        return list(self._handlers)

    # --- study_data ------------------------------------------------------------------------

    async def _data(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = context.user_id
        if user_id is None:
            return ToolResult.failure("no_user", "Данные студента без пользователя недоступны.")
        try:
            args = StudyDataArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        now = aware(self._clock.now())
        try:
            async with self._sm() as session:
                spaces = await spaces_of(session, user_id)
                if args.space_id is not None and all(s.id != args.space_id for s in spaces):
                    return ToolResult.failure(
                        "not_found",
                        "Такого вуза у студента нет.",
                        hint="space_id — из сводки в контексте или space_create/source_list.",
                    )
                zone = _zone_for(spaces, args.space_id)
                try:
                    start = parse_moment(args.from_, zone)
                    end = parse_moment(args.to, zone, end=True)
                except ValueError:
                    return ToolResult.failure(
                        "invalid_arguments",
                        "from/to: нужна ISO-дата (2026-11-12) или дата-время (2026-11-12T10:00).",
                    )
                data: dict[str, Any] = {}
                for kind in args.kinds:
                    rows = await self._read(session, user_id, kind, args, now, zone, start, end)
                    data[kind] = _section(kind, rows, args.limit)
                freshness = await source_freshness(
                    session, user_id, now=now, space_id=args.space_id, spaces=spaces
                )
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        data["sources"] = [source.data() for source in freshness]
        data["now"] = now.astimezone(zone).isoformat(timespec="minutes")
        counts = ", ".join(f"{kind}: {data[kind]['count']}" for kind in args.kinds)
        oldest = max((s.age_minutes for s in freshness if s.age_minutes is not None), default=None)
        summary = f"Сохранённые данные — {counts}."
        if oldest is not None:
            summary += f" Старейший источник обновлён {oldest} мин назад."
        return ToolResult.success(summary=summary, data=data)

    async def _read(
        self,
        session: AsyncSession,
        user_id: str,
        kind: StudyKind,
        args: StudyDataArgs,
        now: datetime,
        zone: ZoneInfo,
        start: datetime | None,
        end: datetime | None,
    ) -> list[dict[str, Any]]:
        """All matching records of one kind as API dicts (the caller cuts to `limit`)."""
        space_id = args.space_id
        if kind == "grades":
            rows = [
                dumped(item)
                for item in await repo.list_grades(session, user_id, space_id=space_id, now=now)
            ]
            return [
                row
                for row in rows
                if _contains(row.get("courseTitle"), args.course)
                and _contains(row.get("title"), args.query)
                and (args.layer is None or (row.get("layer") or "item") == args.layer)
                and _within(row.get("gradedAt") or row.get("changedAt"), start, end)
            ]
        if kind == "deadlines":
            rows = [
                dumped(item)
                for item in await repo.list_deadlines(
                    session, user_id, space_id=space_id, include_done=True, now=now
                )
            ]
            recent = now - timedelta(days=DONE_DAYS)

            def wanted(row: dict[str, Any]) -> bool:
                state = str(row.get("state") or "unknown")
                if args.state is not None and state != args.state:
                    return False
                due = row.get("effectiveDueAt") or row.get("dueAt")
                if start is not None or end is not None:
                    return _within(due, start, end)
                if args.state is not None or state not in DONE_STATES:
                    return True
                # Done work: only what is recent, by its due date or its last change.
                return any(
                    (moment := _moment_of(value)) is not None and moment >= recent
                    for value in (due, row.get("changedAt"))
                )

            return [
                row
                for row in rows
                if _contains(row.get("courseTitle"), args.course)
                and _contains(row.get("title"), args.query)
                and wanted(row)
            ]
        if kind == "schedule":
            # Default window: today and the next seven days, in the university's own calendar.
            lo = start or datetime.combine(now.astimezone(zone).date(), time(), tzinfo=zone)
            hi = end or lo + timedelta(days=SCHEDULE_DAYS + 1)
            rows = [
                dumped(item)
                for item in await repo.list_events(
                    session, user_id, start=lo, end=hi, space_id=space_id, now=now
                )
            ]
            return [
                row
                for row in rows
                if _contains(row.get("courseTitle") or row.get("title"), args.course)
                and _contains(row.get("title"), args.query)
            ]
        if kind == "courses":
            rows = [
                dumped(item)
                for item in await repo.list_courses(session, user_id, space_id=space_id, now=now)
            ]
            return [
                row
                for row in rows
                if _contains(row.get("title"), args.course)
                and _contains(row.get("title"), args.query)
            ]
        if kind == "profile":
            rows = [
                dumped(item)
                for item in await repo.list_profile(session, user_id, space_id=space_id, now=now)
            ]
            return [
                row
                for row in rows
                if _contains(f"{row.get('label')} {row.get('value')}", args.query)
            ]
        if kind == "ranking":
            rows = [
                dumped(item)
                for item in await repo.list_ranking(session, user_id, space_id=space_id, now=now)
            ]
            return [row for row in rows if _contains(row.get("board"), args.query)]
        since = start or now - timedelta(days=UPDATES_DAYS)
        rows = [
            dumped(item)
            for item in await repo.list_updates(
                session, user_id, space_id=space_id, since=since, limit=MAX_LIMIT
            )
        ]
        return [
            row
            for row in rows
            if _contains(row.get("courseTitle"), args.course)
            and _contains(row.get("title"), args.query)
            and (end is None or _within(row.get("changedAt"), None, end))
        ]

    # --- corrections -----------------------------------------------------------------------

    async def _status_rule(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = context.user_id
        if user_id is None:
            return ToolResult.failure("no_user", "Правила без пользователя невозможны.")
        try:
            args = StatusRuleArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        try:
            async with self._sm() as session:
                if args.action == "list":
                    rules = await overrides.list_status_rules(session, user_id)
                    return ToolResult.success(
                        summary=f"Правил статуса: {len(rules)}.",
                        data={"rules": [_rule_data(rule) for rule in rules]},
                    )
                if args.action == "delete":
                    if not args.rule_id:
                        return ToolResult.failure("invalid_arguments", "delete требует rule_id.")
                    rule = await session.scalar(
                        select(StatusRule).where(
                            StatusRule.id == args.rule_id, StatusRule.user_id == user_id
                        )
                    )
                    before = await _states(session, user_id, rule) if rule is not None else {}
                    await overrides.delete_status_rule(session, user_id, args.rule_id)
                    changed = await _changed(session, before)
                    await session.commit()
                    return ToolResult.success(
                        summary=f"Правило удалено; заданий пересчитано: {len(before)}, "
                        f"изменилось: {changed}.",
                        data={"ruleId": args.rule_id, "tasks": len(before), "changed": changed},
                    )
                if not args.label or args.state is None:
                    return ToolResult.failure(
                        "invalid_arguments",
                        "set требует label (слова статуса из stateText) и state.",
                    )
                probe = StatusRule(
                    user_id=user_id,
                    source_id=args.source_id,
                    label=overrides.normalize_label(args.label),
                    state=args.state,
                )
                before = await _states(session, user_id, probe)
                rule = await overrides.set_status_rule(
                    session,
                    user_id,
                    source_id=args.source_id,
                    label=args.label,
                    state=args.state,
                )
                changed = await _changed(session, before)
                data = {**_rule_data(rule), "tasks": len(before), "changed": changed}
                await session.commit()
        except BotaiError as exc:
            return ToolResult.failure(exc.code.value, exc.detail)
        where = "в этом источнике" if args.source_id else "во всех источниках"
        return ToolResult.success(
            summary=(
                f"Правило сохранено: «{data['label']}» {where} → {args.state}. Заданий с этим "
                f"статусом: {len(before)}, изменилось: {changed}."
            ),
            data=data,
        )

    async def _mark_task(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        user_id = context.user_id
        if user_id is None:
            return ToolResult.failure("no_user", "Отметки без пользователя невозможны.")
        try:
            args = MarkTaskArgs.model_validate(arguments)
        except ValidationError as exc:
            return _invalid(exc)
        try:
            async with self._sm() as session:
                row = await overrides.set_user_state(
                    session, user_id, args.deadline_id, args.state, now=self._clock.now()
                )
                data = {
                    "deadlineId": row.id,
                    "title": clip(row.title, TEXT_CHARS),
                    "userState": row.user_state,
                    "sourceState": row.state,
                    "state": row.user_state or row.state,
                }
                if row.user_state:
                    data["stateSource"] = "student"
                await session.commit()
        except BotaiError as exc:
            return ToolResult.failure(
                exc.code.value, exc.detail, hint="deadline_id — из study_data(kinds=[deadlines])."
            )
        summary = (
            f"Отметка снята: «{data['title']}» снова по источнику ({data['state']})."
            if args.state is None
            else f"Задание «{data['title']}» отмечено: {args.state}."
        )
        return ToolResult.success(summary=summary, data=data)


def _zone_for(spaces: list[SpaceRef], space_id: str | None) -> ZoneInfo:
    for space in spaces:
        if space.id == space_id:
            return space.zone
    return spaces[0].zone if spaces else zone_of(None)


def _rule_data(rule: StatusRule) -> dict[str, Any]:
    return {
        "ruleId": rule.id,
        "sourceId": rule.source_id,
        "label": rule.label,
        "state": rule.state,
    }


async def _states(session: AsyncSession, user_id: str, rule: StatusRule | None) -> dict[str, str]:
    """Current states of the tasks a rule covers (same label, its source or all v4 sources)."""
    if rule is None:
        return {}
    stmt = select(Deadline.id, Deadline.state, Deadline.state_text).where(
        Deadline.user_id == user_id,
        Deadline.source_id.is_not(None),
        Deadline.state_text.is_not(None),
    )
    if rule.source_id is not None:
        stmt = stmt.where(Deadline.source_id == rule.source_id)
    return {
        deadline_id: state
        for deadline_id, state, text in (await session.execute(stmt)).all()
        if overrides.normalize_label(text) == rule.label
    }


async def _changed(session: AsyncSession, before: Mapping[str, str]) -> int:
    if not before:
        return 0
    rows = (
        await session.execute(
            select(Deadline.id, Deadline.state).where(Deadline.id.in_(list(before)))
        )
    ).all()
    return sum(1 for deadline_id, state in rows if before.get(deadline_id) != state)


def build_study_tools(toolkit: StudyToolkit) -> list[ToolHandler]:
    """The study set; the wiring adds it next to `source_*` when the v4 engine is on."""
    return toolkit.tools()


__all__ = ["STUDY_KINDS", "StudyToolkit", "build_study_tools", "parse_moment"]
