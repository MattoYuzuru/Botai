"""Field vocabulary per kind: the only names a spec may map and the Normalizer understands.

This is the personal-data firewall for what flows into the domain tables: a spec cannot extract
a field that is not listed here (the lint rejects it), so profile data such as a passport number
has no column to land in.
"""

from __future__ import annotations

from typing import Literal

from botai.contracts.specs import Kind

# Kinds that exist only in v4 (docs/v4/23-panels-backend.md): the v3 recipe language and its
# schemas (`contracts/specs.Kind`, `recipe.schema.json`) stay exactly as they were. The v3 tables
# below (`VOCAB`, `REQUIRED`, `KEY_FIELDS`, `KIND_LABELS`) are iterated by the v3 learner, so the
# new kinds live in their own tables and are merged into the `ALL_*` views the v4 runner reads.
V4Kind = Literal["profile", "ranking"]
DomainKind = Kind | V4Kind

VOCAB: dict[Kind, frozenset[str]] = {
    "courses": frozenset({"externalId", "title", "code", "term", "description", "url", "archived"}),
    "scheduleEvents": frozenset(
        {
            "externalId",
            "title",
            "kind",
            "courseId",
            "startsAt",
            "endsAt",
            "durationMinutes",
            "format",
            "location",
            "teacher",
            "url",
            "kindText",
            "address",
            "group",
            "lessonNumber",
            "links",
            "attributes",
        }
    ),
    "assignments": frozenset({"externalId", "title", "courseId", "description", "url", "maxScore"}),
    "deadlines": frozenset(
        {
            "externalId",
            "title",
            "courseId",
            "assignmentId",
            "dueAt",
            "effectiveDueAt",
            "state",
            "url",
        }
    ),
    "grades": frozenset(
        {"externalId", "title", "courseId", "value", "scale", "weight", "gradedAt"}
    ),
    "people": frozenset({"externalId", "name", "role", "email", "url"}),
    "locations": frozenset({"externalId", "title", "building", "address"}),
    "news": frozenset({"externalId", "title", "body", "publishedAt", "url"}),
}

# Fields without which a record is useless; the Normalizer drops records missing them.
REQUIRED: dict[Kind, frozenset[str]] = {
    "courses": frozenset({"title"}),
    "scheduleEvents": frozenset({"title", "startsAt"}),
    "assignments": frozenset({"title"}),
    "deadlines": frozenset({"title", "dueAt"}),
    "grades": frozenset({"value"}),
    "people": frozenset({"name"}),
    "locations": frozenset({"title"}),
    "news": frozenset({"title", "publishedAt"}),
}

# Key fields compared by the canary against the last confirmed snapshot.
KEY_FIELDS: dict[Kind, tuple[str, ...]] = {
    "courses": ("title",),
    "scheduleEvents": ("title", "startsAt"),
    "assignments": ("title",),
    "deadlines": ("title", "dueAt"),
    "grades": ("title", "value"),
    "people": ("name",),
    "locations": ("title",),
    "news": ("title",),
}

# Structured values of an event: they come from the response's `links` and `attributes`
# sections, never from a `fields` entry or a document column.
STRUCTURED_FIELDS: frozenset[str] = frozenset({"links", "attributes"})

TABLE_KINDS: tuple[Kind, ...] = ("scheduleEvents", "deadlines", "grades")
# Extra column targets for document tables that keep the date and the time in separate columns.
TABLE_EXTRA_FIELDS: frozenset[str] = frozenset({"date", "startTime", "endTime"})

KIND_LABELS: dict[Kind, str] = {
    "courses": "Курсы",
    "scheduleEvents": "Расписание",
    "assignments": "Задания",
    "deadlines": "Дедлайны",
    "grades": "Оценки",
    "people": "Преподаватели",
    "locations": "Аудитории",
    "news": "Новости",
}

# --- v4-only kinds: a student card and a rating board (D-039) --------------------------------------

V4_VOCAB: dict[V4Kind, frozenset[str]] = {
    # One row per shown field of the student's card: the key the source uses, its caption, the value
    # exactly as the source printed it (never parsed), and an optional group of the card.
    "profile": frozenset({"externalId", "field", "label", "value", "group"}),
    # One row per place of one board. `score` is the source's own text, `scoreNum` its number when
    # unambiguous; nothing here is computed by the application.
    "ranking": frozenset(
        {
            "externalId",
            "board",
            "position",
            "label",
            "score",
            "scoreNum",
            "isMe",
            "total",
            "period",
        }
    ),
}
V4_REQUIRED: dict[V4Kind, frozenset[str]] = {
    "profile": frozenset({"field", "value"}),
    "ranking": frozenset({"board", "position", "label"}),
}
V4_KEY_FIELDS: dict[V4Kind, tuple[str, ...]] = {
    "profile": ("field", "value"),
    "ranking": ("board", "position", "label"),
}
V4_KIND_LABELS: dict[V4Kind, str] = {"profile": "Профиль", "ranking": "Рейтинг"}

# What the v4 runner and the agent's source protocol read: the v3 kinds plus the v4-only ones.
ALL_VOCAB: dict[DomainKind, frozenset[str]] = {**VOCAB, **V4_VOCAB}
ALL_REQUIRED: dict[DomainKind, frozenset[str]] = {**REQUIRED, **V4_REQUIRED}
ALL_KEY_FIELDS: dict[DomainKind, tuple[str, ...]] = {**KEY_FIELDS, **V4_KEY_FIELDS}
ALL_KIND_LABELS: dict[DomainKind, str] = {**KIND_LABELS, **V4_KIND_LABELS}
