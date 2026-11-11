"""Normalizer: an `AdapterResult` -> domain rows, idempotently.

Rules (docs/architecture/lld.md §3):

* identity is `(connection_id, external_id)`; running the same result twice changes nothing but
  `fetched_at` and `seen_at`;
* every row records where and when it came from (`source_url`, `fetched_at`, `spec_revision`)
  and `seen_at`, the last sync that saw it;
* rows that a complete run did not see are never deleted. They keep an old `seen_at`, so
  "missing" means `seen_at < connection.last_sync_at`. Besides that, a class that was due in the
  future and vanished gets `changed = true`, a vanished course is `archived`. The exception is an
  occurrence of a repeating rule (`OCCURRENCE_PREFIX`): the source never listed it, the rule
  produced it, so when the rule stops saying it, a future occurrence is removed, while a past one
  stays as it happened;
* only kinds whose every endpoint finished may mark rows missing: a partial failure never
  touches the previous snapshot of a kind.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from botai.contracts.specs import Kind
from botai.db import models as m
from botai.sources.adapter import OCCURRENCE_PREFIX, AdapterResult, ExtractedDocument, Record
from botai.sources.extract import clean_text, parse_datetime
from botai.sources.grade_scale import format_number, grade_numbers, plain_number, scale_max_of
from botai.sources.states import STATES, task_state
from botai.sources.vocab import ALL_REQUIRED, GRADE_LAYERS, DomainKind

MODELS: dict[Kind, type[Any]] = {
    "courses": m.Course,
    "scheduleEvents": m.ScheduleEvent,
    "assignments": m.Assignment,
    "deadlines": m.Deadline,
    "grades": m.Grade,
    "people": m.Person,
    "locations": m.Location,
    "news": m.NewsItem,
}
# What the v4 domain writer reads: the v3 tables plus the v4-only kinds (the v3 recipe language
# does not know them, `sources.vocab.V4Kind`; the v3 loops over `MODELS` stay as they were).
ALL_MODELS: dict[DomainKind, type[Any]] = {
    **MODELS,
    "profile": m.ProfileField,
    "ranking": m.RankingEntry,
}
# Columns whose change means "the student should notice" for a class.
_EVENT_MATERIAL = (
    "title",
    "kind",
    "starts_at",
    "ends_at",
    "location_text",
    "teacher_text",
    "address",
)
_CHUNK = 500


@dataclass(slots=True)
class KindStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    invalid: int = 0
    missing: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "invalid": self.invalid,
            "missing": self.missing,
        }


# --- value coercion -------------------------------------------------------------------------

_EVENT_KINDS = (
    ("exam", ("exam", "экзамен", "зачёт", "зачет", "credit")),
    ("consultation", ("consult", "консульт")),
    ("lab", ("lab", "лаборатор")),
    ("seminar", ("semin", "семинар", "практи", "practic")),
    ("lecture", ("lectur", "лекц")),
)
_FORMATS = (
    ("hybrid", ("hybrid", "гибрид", "смешан")),
    ("online", ("online", "онлайн", "дистанц", "remote")),
    ("offline", ("offline", "очно", "очная", "очный")),
)
_ROLES = (
    ("assistant", ("assistant", "ассистент", "ta")),
    ("teacher", ("teacher", "lecturer", "professor", "преподават", "лектор", "доцент")),
    ("staff", ("staff", "сотрудник", "методист")),
)


def _pick(value: Any, table: Iterable[tuple[str, tuple[str, ...]]], default: str) -> str:
    if not isinstance(value, str):
        return default
    low = value.casefold()
    for label, stems in table:
        if any(stem in low for stem in stems):
            return label
    return default


def _text(value: Any, limit: int) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    out = clean_text(str(value), limit).strip()
    return out or None


def _dt(value: Any, tz: ZoneInfo) -> datetime | None:
    return parse_datetime(value, tz)


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _url(value: Any, base: str) -> str | None:
    """Only http(s) links without credentials; relative ones are resolved against the source."""
    if not isinstance(value, str) or not value.strip():
        return None
    joined = urljoin(base, value.strip())
    parts = urlsplit(joined)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        return None
    return joined[:2000]


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and 0 <= value <= 10_000 else None


MAX_LINKS = 4
MAX_ATTRIBUTES = 8


def _links(value: Any, base: str) -> list[dict[str, str]]:
    """Online rooms and materials: http(s) links only, each with a short readable label."""
    out: list[dict[str, str]] = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, Mapping):
            continue
        url = _url(raw.get("url"), base)
        if url is None or any(item["url"] == url for item in out):
            continue
        out.append({"url": url, "label": _text(raw.get("label"), 80) or ""})
        if len(out) == MAX_LINKS:
            break
    return out


def _attributes(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, Mapping):
            continue
        label, text_ = _text(raw.get("label"), 40), _text(raw.get("value"), 200)
        if label is not None and text_ is not None:
            out.append({"label": label, "value": text_})
        if len(out) == MAX_ATTRIBUTES:
            break
    return out


# What a source or a playbook's code prints for "no value": it must not become a literal "None".
_NO_VALUE = frozenset({"none", "null", "nil", "nan", "undefined", "n/a", "-", "—", "–", "н/д"})


def _present(value: str | None) -> str | None:
    """The text, or None when it only says that there is no value."""
    if value is None or value.casefold() in _NO_VALUE:
        return None
    return value


def _grade_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return _present(_text(value, 60))


def _position(value: Any, *, high: int) -> int | None:
    """A whole number from 1 to `high`: a place or a size of a list."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and 1 <= value <= high else None


_TRUE = frozenset({"true", "yes", "да", "1"})


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().casefold() in _TRUE


# A card field that holds a secret is not a student's data: it is dropped, never stored.
_SECRET_FIELD = re.compile(
    r"парол|password|passwd|token|токен|secret|секрет|\bcvv\b|\bcvc\b|\botp\b|api[-_ ]?key|"
    r"пин[- ]?код|одноразов",
    re.IGNORECASE,
)


def _card_text(value: Any, limit: int) -> str | None:
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    return _present(_text(value, limit))


Coercer = Callable[[Mapping[str, Any], str, ZoneInfo], dict[str, Any] | None]


def _course(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    return {
        "title": _text(f.get("title"), 300),
        "code": _text(f.get("code"), 60),
        "term": _text(f.get("term"), 60),
        "description": _text(f.get("description"), 20000),
        "url": _url(f.get("url"), src),
        "archived": bool(f.get("archived")) if isinstance(f.get("archived"), bool) else False,
    }


def _event(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    start = _dt(f.get("startsAt"), tz)
    end = _dt(f.get("endsAt"), tz)
    minutes = f.get("durationMinutes")
    if end is None and start is not None and isinstance(minutes, (int, float)) and minutes > 0:
        end = start + timedelta(minutes=float(minutes))
    if start is None or end is None or end < start:
        return None
    return {
        "title": _text(f.get("title"), 300),
        "kind": _pick(f.get("kind"), _EVENT_KINDS, "other"),
        "course_external_id": _text(f.get("courseId"), 200),
        "starts_at": start,
        "ends_at": end,
        "format": _pick(f.get("format"), _FORMATS, "unknown"),
        "location_text": _text(f.get("location"), 200),
        "teacher_text": _text(f.get("teacher"), 300),
        "url": _url(f.get("url"), src),
        "kind_text": _text(f.get("kindText"), 120),
        "address": _text(f.get("address"), 300),
        "group_text": _text(f.get("group"), 200),
        "lesson_number": _int(f.get("lessonNumber")),
        "links": _links(f.get("links"), src),
        "attributes": _attributes(f.get("attributes")),
    }


def _assignment(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    return {
        "title": _text(f.get("title"), 300),
        "course_external_id": _text(f.get("courseId"), 200),
        "description": _text(f.get("description"), 20000),
        "url": _url(f.get("url"), src),
        "max_score": _float(f.get("maxScore")),
    }


def task_status(state: Any, state_text: Any) -> tuple[str, str | None]:
    """`(state, state_text)` of a task (D-043 §3.3).

    `state` of the closed set is taken as it is; anything else is the source's own word and goes to
    `state_text` (unless the parser gave one), the state then read from the words by
    `task_state`."""
    text_ = _present(_text(state_text, 120))
    if isinstance(state, str) and state.strip().casefold() in STATES:
        return state.strip().casefold(), text_
    raw = _present(_text(state, 120)) if isinstance(state, str) else None
    derived = task_state(text_) if text_ is not None else "unknown"
    if derived == "unknown":
        derived = task_state(state)
    return derived, text_ or raw


def _deadline(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    due = _dt(f.get("dueAt"), tz)
    if due is None:
        return None
    state, state_text = task_status(f.get("state"), f.get("stateText"))
    return {
        "title": _text(f.get("title"), 300),
        "course_external_id": _text(f.get("courseId"), 200),
        "assignment_external_id": _text(f.get("assignmentId"), 200),
        "due_at": due,
        "effective_due_at": _dt(f.get("effectiveDueAt"), tz),
        "state": state,
        "state_text": state_text,
        "url": _url(f.get("url"), src),
    }


def _layer(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    layer = value.strip().casefold()
    return layer if layer in GRADE_LAYERS else None


MAX_GRADE_COMMENT = 2000


def _grade(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    value = _grade_value(f.get("value"))
    scale = _present(_text(f.get("scale"), 60))
    top = scale_max_of(f.get("max"))
    if scale is None and top is not None:
        scale = f"из {format_number(top)}"
    # Numbers only when the source gave both plainly; never averaged or converted (D-039).
    value_num, scale_max = grade_numbers(value, scale, top)
    return {
        "title": _present(_text(f.get("title"), 300)),
        "course_external_id": _text(f.get("courseId"), 200),
        "value": value,
        "scale": scale,
        "weight": _float(f.get("weight")),
        "graded_at": _dt(f.get("gradedAt"), tz),
        "value_num": value_num,
        "scale_max": scale_max,
        # the v4 fields (`vocab.V4_EXTRA_FIELDS`); a v3 recipe never maps them, so they stay NULL
        "layer": _layer(f.get("layer")),
        "period": _present(_text(f.get("period"), 60)),
        "assignment_external_id": _text(f.get("assignmentId"), 200),
        "url": _url(f.get("url"), src),
        "comment": _present(_text(f.get("comment"), MAX_GRADE_COMMENT)),
    }


def _person(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    return {
        "name": _text(f.get("name"), 200),
        "role": _pick(f.get("role"), _ROLES, "other"),
        "email": _text(f.get("email"), 320),
        "url": _url(f.get("url"), src),
    }


def _location(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    return {
        "title": _text(f.get("title"), 200),
        "building": _text(f.get("building"), 200),
        "address": _text(f.get("address"), 300),
    }


def _news(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    published = _dt(f.get("publishedAt"), tz)
    if published is None:
        return None
    return {
        "title": _text(f.get("title"), 300),
        "body": _text(f.get("body"), 20000),
        "published_at": published,
        "url": _url(f.get("url"), src),
    }


def _profile(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    """One field of the student's card: the value stays the text the source printed."""
    field = _text(f.get("field"), 100)
    label = _card_text(f.get("label"), 200) or field
    if field is None or _SECRET_FIELD.search(f"{field} {label}"):
        return None
    return {
        "field": field,
        "label": label,
        "value": _card_text(f.get("value"), 500),
        "group_text": _card_text(f.get("group"), 100),
    }


def _ranking(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    """One place of a rating board; `score_num` only when the source gave a plain number."""
    score = _present(_text(f.get("score"), 60))
    score_num = plain_number(f.get("scoreNum"))
    if score_num is None:
        score_num = plain_number(score)
    return {
        "board": _text(f.get("board"), 120),
        "position": _position(f.get("position"), high=1_000_000),
        "label": _text(f.get("label"), 300),
        "score": score,
        "score_num": score_num,
        "is_me": _flag(f.get("isMe")),
        "total": _position(f.get("total"), high=10_000_000),
        "period": _present(_text(f.get("period"), 60)),
    }


COERCERS: dict[DomainKind, Coercer] = {
    "courses": _course,
    "scheduleEvents": _event,
    "assignments": _assignment,
    "deadlines": _deadline,
    "grades": _grade,
    "people": _person,
    "locations": _location,
    "news": _news,
    "profile": _profile,
    "ranking": _ranking,
}
# Columns that must be non-empty after coercion, per kind (the model's NOT NULL columns).
_REQUIRED_COLUMNS: dict[DomainKind, tuple[str, ...]] = {
    "courses": ("title",),
    "scheduleEvents": ("title", "starts_at", "ends_at"),
    "assignments": ("title",),
    "deadlines": ("title", "due_at"),
    "grades": ("value",),
    "people": ("name",),
    "locations": ("title",),
    "news": ("title", "published_at"),
    "profile": ("field", "label", "value"),
    "ranking": ("board", "position", "label"),
}


def _open_url(record: Record) -> str | None:
    return _url(record.open_url, record.source_url)


def coerce_fields(
    kind: DomainKind, fields: Mapping[str, Any], source_url: str, tz: ZoneInfo
) -> dict[str, Any] | None:
    """Domain columns for the fields of a record, or None when it lacks what the kind needs.

    The v4 runner calls this directly: its kinds include `profile` and `ranking`, which no v3
    `Record` can carry."""
    if not ALL_REQUIRED[kind] <= {k for k, v in fields.items() if v is not None}:
        return None
    row = COERCERS[kind](fields, source_url, tz)
    if row is None or any(row.get(col) is None for col in _REQUIRED_COLUMNS[kind]):
        return None
    return row


def coerce(kind: Kind, record: Record, tz: ZoneInfo) -> dict[str, Any] | None:
    """Domain columns for a record, or None when it lacks what the kind cannot do without."""
    return coerce_fields(kind, record.fields, record.source_url, tz)


# --- persistence ----------------------------------------------------------------------------


async def lock_connection(session: AsyncSession, connection_id: str) -> None:
    """Serialise writers of one connection for the rest of the transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"sync:{connection_id}"}
    )


async def _existing(
    session: AsyncSession, model: type[Any], connection_id: str, ids: list[str]
) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for i in range(0, len(ids), _CHUNK):
        part = ids[i : i + _CHUNK]
        rows = await session.execute(
            select(model).where(model.connection_id == connection_id, model.external_id.in_(part))
        )
        for obj in rows.scalars():
            found[obj.external_id] = obj
    return found


async def apply_result(
    session: AsyncSession,
    *,
    user_id: str,
    connection_id: str,
    result: AdapterResult,
    now: datetime,
    tz: ZoneInfo,
    mark_missing: set[Kind] | None = None,
) -> dict[Kind, KindStats]:
    """Upsert every record of `result`; mark absent rows only for the kinds in `mark_missing`
    (default: the kinds the adapter completed)."""
    await lock_connection(session, connection_id)
    missing_kinds = result.complete_kinds() if mark_missing is None else mark_missing
    stats: dict[Kind, KindStats] = {}
    by_kind: dict[Kind, list[Record]] = {}
    for rec in result.records:
        by_kind.setdefault(rec.kind, []).append(rec)
    touched = set(by_kind) | missing_kinds
    for kind in touched:
        model = MODELS[kind]
        st = stats.setdefault(kind, KindStats())
        rows: dict[str, tuple[Record, dict[str, Any]]] = {}
        for rec in by_kind.get(kind, []):
            row = coerce(kind, rec, tz)
            if row is None:
                st.invalid += 1
            else:
                rows[rec.external_id] = (rec, row)
        existing = await _existing(session, model, connection_id, list(rows))
        for external_id, (rec, row) in rows.items():
            obj = existing.get(external_id)
            if obj is None:
                session.add(
                    model(
                        user_id=user_id,
                        connection_id=connection_id,
                        external_id=external_id,
                        source_url=rec.source_url,
                        open_url=_open_url(rec),
                        fetched_at=now,
                        spec_revision=result.spec_revision,
                        seen_at=now,
                        **row,
                    )
                )
                st.inserted += 1
                continue
            changed = [col for col, val in row.items() if getattr(obj, col) != val]
            # A class that gains an address it never had (a recipe that learned a new field) did
            # not move; only a different value is a change the student should notice.
            material = [
                col
                for col in changed
                if col in _EVENT_MATERIAL and not (col == "address" and getattr(obj, col) is None)
            ]
            for col in changed:
                setattr(obj, col, row[col])
            if kind == "scheduleEvents" and material:
                obj.changed = True
            obj.source_url, obj.open_url, obj.fetched_at = rec.source_url, _open_url(rec), now
            obj.spec_revision, obj.seen_at = result.spec_revision, now
            st.updated += 1 if changed else 0
            st.unchanged += 0 if changed else 1
        if kind in missing_kinds:
            st.missing = await _mark_missing(
                session, kind, connection_id, now, _expansion_window(result, kind)
            )
    await session.flush()
    return stats


def _expansion_window(result: AdapterResult, kind: Kind) -> tuple[datetime, datetime] | None:
    """Where the weekly rules of this kind spoke in this read: the days all of them looked at."""
    windows = [o.expanded for o in result.outcomes if o.kind == kind and o.expanded is not None]
    if not windows:
        return None
    start, end = max(w[0] for w in windows), min(w[1] for w in windows)
    return (start, end) if start < end else None


async def _mark_missing(
    session: AsyncSession,
    kind: Kind,
    connection_id: str,
    now: datetime,
    window: tuple[datetime, datetime] | None = None,
) -> int:
    model = MODELS[kind]
    rows = (
        (
            await session.execute(
                select(model).where(model.connection_id == connection_id, model.seen_at < now)
            )
        )
        .scalars()
        .all()
    )
    missing = 0
    for obj in rows:
        ahead = kind == "scheduleEvents" and obj.starts_at >= now
        if ahead and obj.external_id.startswith(OCCURRENCE_PREFIX):
            # The rules are silent beyond the days they looked at (the horizon slides forward,
            # a repair may shorten it): only what lies inside this read's horizon is gone.
            if window is None or not window[0] <= obj.starts_at < window[1]:
                continue
            await session.delete(obj)  # the rules no longer produce it
        elif ahead:
            obj.changed = True  # was still ahead and is gone: moved or cancelled
        elif kind == "courses":
            obj.archived = True
        missing += 1
    return missing


# --- documents ------------------------------------------------------------------------------


def _tokens(text_: str) -> int:
    return max(1, len(text_) // 4)


async def store_document(
    session: AsyncSession,
    *,
    user_id: str,
    space_id: str,
    connection_id: str,
    doc: ExtractedDocument,
    now: datetime,
) -> str:
    """One document per document connection: a changed file replaces the old text and chunks."""
    await lock_connection(session, connection_id)
    current = (
        (
            await session.execute(
                select(m.Document).where(
                    m.Document.connection_id == connection_id, m.Document.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    for old in current:
        if old.hash == doc.hash:
            old.fetched_at = now
            await session.flush()
            return old.id
    for old in current:
        await session.delete(old)  # chunks follow by ON DELETE CASCADE
    await session.flush()
    row = m.Document(
        user_id=user_id,
        space_id=space_id,
        connection_id=connection_id,
        kind=doc.kind,
        title=doc.title[:300],
        url=doc.url,
        mime=doc.mime,
        fetched_at=now,
        hash=doc.hash,
    )
    session.add(row)
    await session.flush()
    for i, piece in enumerate(doc.chunks):
        session.add(
            m.Chunk(
                user_id=user_id,
                document_id=row.id,
                ord=i,
                text=piece,
                tokens=_tokens(piece),
            )
        )
    await session.flush()
    return row.id
