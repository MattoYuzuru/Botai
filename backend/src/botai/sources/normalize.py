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
from botai.sources.vocab import REQUIRED

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
_STATES = (
    ("graded", ("graded", "evaluat", "оценен", "проверен")),
    ("missed", ("missed", "overdue", "failed", "просроч", "провал")),
    (
        "open",
        (
            "open",
            "new",
            "pending",
            "todo",
            "assigned",
            "backlog",
            "inprogress",
            "in progress",
            "in_progress",
            "не сдан",
            "назначен",
            "активн",
        ),
    ),
    ("submitted", ("submitted", "done", "completed", "review", "сдан", "выполнен", "на проверке")),
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


def _grade_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return _text(value, 60)


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


def _deadline(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    due = _dt(f.get("dueAt"), tz)
    if due is None:
        return None
    return {
        "title": _text(f.get("title"), 300),
        "course_external_id": _text(f.get("courseId"), 200),
        "assignment_external_id": _text(f.get("assignmentId"), 200),
        "due_at": due,
        "effective_due_at": _dt(f.get("effectiveDueAt"), tz),
        "state": _pick(f.get("state"), _STATES, "unknown"),
        "url": _url(f.get("url"), src),
    }


def _grade(f: Mapping[str, Any], src: str, tz: ZoneInfo) -> dict[str, Any] | None:
    return {
        "title": _text(f.get("title"), 300),
        "course_external_id": _text(f.get("courseId"), 200),
        "value": _grade_value(f.get("value")),
        "scale": _text(f.get("scale"), 60),
        "weight": _float(f.get("weight")),
        "graded_at": _dt(f.get("gradedAt"), tz),
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


COERCERS: dict[Kind, Coercer] = {
    "courses": _course,
    "scheduleEvents": _event,
    "assignments": _assignment,
    "deadlines": _deadline,
    "grades": _grade,
    "people": _person,
    "locations": _location,
    "news": _news,
}
# Columns that must be non-empty after coercion, per kind (the model's NOT NULL columns).
_REQUIRED_COLUMNS: dict[Kind, tuple[str, ...]] = {
    "courses": ("title",),
    "scheduleEvents": ("title", "starts_at", "ends_at"),
    "assignments": ("title",),
    "deadlines": ("title", "due_at"),
    "grades": ("value",),
    "people": ("name",),
    "locations": ("title",),
    "news": ("title", "published_at"),
}


def _open_url(record: Record) -> str | None:
    return _url(record.open_url, record.source_url)


def coerce(kind: Kind, record: Record, tz: ZoneInfo) -> dict[str, Any] | None:
    """Domain columns for a record, or None when it lacks what the kind cannot do without."""
    if not REQUIRED[kind] <= {k for k, v in record.fields.items() if v is not None}:
        return None
    row = COERCERS[kind](record.fields, record.source_url, tz)
    if row is None or any(row.get(col) is None for col in _REQUIRED_COLUMNS[kind]):
        return None
    return row


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
            st.missing = await _mark_missing(session, kind, connection_id, now)
    await session.flush()
    return stats


async def _mark_missing(
    session: AsyncSession, kind: Kind, connection_id: str, now: datetime
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
    for obj in rows:
        if kind == "courses":
            obj.archived = True
        elif kind == "scheduleEvents" and obj.starts_at >= now:
            if obj.external_id.startswith(OCCURRENCE_PREFIX):
                await session.delete(obj)  # the rule no longer produces it
            else:
                obj.changed = True  # was still ahead and is gone: moved or cancelled
    return len(rows)


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
