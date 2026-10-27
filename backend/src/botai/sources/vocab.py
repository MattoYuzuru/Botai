"""Field vocabulary per kind: the only names a spec may map and the Normalizer understands.

This is the personal-data firewall for what flows into the domain tables: a spec cannot extract
a field that is not listed here (the lint rejects it), so profile data such as a passport number
has no column to land in.
"""

from __future__ import annotations

from botai.contracts.specs import Kind

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
