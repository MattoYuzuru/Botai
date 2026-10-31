"""The v4 vocabulary of source kinds, statuses, trust, playbook languages and artifact kinds.

One source may bring several kinds at once: a nature (what the access point is) and data (what is
read from it). The vocabulary is shared with the UI and the agent; the database repeats the
status/trust/artifact lists in CHECK constraints (see `db.models.SOURCE_*` and the migration), so a
value the service accepts always fits the column.

Legacy v3 `connections.kind` values are translated for the future migration; `scheduleEvents` is
the reading vocabulary of recipes and maps one-to-one to the `schedule` kind.
"""

from __future__ import annotations

from collections.abc import Iterable

from botai.contracts.specs import Kind
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import (
    ARTIFACT_KINDS,
    PLAYBOOK_LANGUAGES,
    SOURCE_DATA_KINDS,
    SOURCE_NATURE_KINDS,
    SOURCE_STATUSES,
    SOURCE_TRUST_LEVELS,
)

__all__ = [
    "ALL_KINDS",
    "ARTIFACT_KINDS",
    "DATA_KINDS",
    "KIND_TO_READING",
    "LEGACY_KIND_ALIASES",
    "NATURE_KINDS",
    "PLAYBOOK_LANGUAGES",
    "READING_KINDS",
    "SOURCE_STATUSES",
    "SOURCE_TRUST_LEVELS",
    "data_kinds",
    "kinds_from_legacy",
    "nature_kinds",
    "normalize_kinds",
    "reading_kind",
    "v4_kind",
    "validate_artifact_kind",
    "validate_language",
    "validate_status",
    "validate_trust",
]

NATURE_KINDS: tuple[str, ...] = SOURCE_NATURE_KINDS
DATA_KINDS: tuple[str, ...] = SOURCE_DATA_KINDS
ALL_KINDS: tuple[str, ...] = NATURE_KINDS + DATA_KINDS

# The reading vocabulary of recipes and the domain write: a v4 data kind -> the v3 kind whose
# domain table and coercion it shares. A kind missing here has no domain table yet (documents,
# messages): its records stay in the run report until a domain home exists.
READING_KINDS: dict[str, Kind] = {
    "schedule": "scheduleEvents",
    "deadlines": "deadlines",
    "assignments": "assignments",
    "courses": "courses",
    "grades": "grades",
    "people": "people",
    "locations": "locations",
    "news": "news",
}
KIND_TO_READING: dict[Kind, str] = {kind: name for name, kind in READING_KINDS.items()}


def reading_kind(kind: str) -> Kind | None:
    """The v3/domain kind of a v4 data kind name, or None when it has no domain table."""
    value = kind.strip()
    return READING_KINDS.get(value)


def v4_kind(kind: Kind) -> str:
    """The v4 data kind name of a v3/domain kind (the inverse of `reading_kind`)."""
    return KIND_TO_READING[kind]


# v3 kinds that do not exist in v4, and the recipe vocabulary of reading capabilities.
LEGACY_KIND_ALIASES: dict[str, str] = {
    "document": "file",
    "scheduleEvents": "schedule",
    "deadlines": "deadlines",
    "assignments": "assignments",
    "courses": "courses",
    "grades": "grades",
    "people": "people",
    "locations": "locations",
    "news": "news",
}


def _fail(detail: str, *, extra: dict[str, str] | None = None) -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, detail, extra=extra)


def normalize_kinds(values: Iterable[str]) -> list[str]:
    """Deduplicate and validate kinds, keeping the caller's order (nature first is not required)."""
    result: list[str] = []
    for raw in values:
        kind = raw.strip()
        if not kind:
            continue
        if kind not in ALL_KINDS:
            raise _fail(f"Неизвестный вид источника: {kind}")
        if kind not in result:
            result.append(kind)
    return result


def nature_kinds(values: Iterable[str]) -> list[str]:
    kinds = normalize_kinds(values)
    return [kind for kind in kinds if kind in NATURE_KINDS]


def data_kinds(values: Iterable[str]) -> list[str]:
    kinds = normalize_kinds(values)
    return [kind for kind in kinds if kind in DATA_KINDS]


def validate_status(status: str) -> str:
    value = status.strip()
    if value not in SOURCE_STATUSES:
        raise _fail(f"Неизвестное состояние источника: {value}")
    return value


def validate_trust(trust: str) -> str:
    value = trust.strip()
    if value not in SOURCE_TRUST_LEVELS:
        raise _fail(f"Неизвестное доверие источника: {value}")
    return value


def validate_language(language: str) -> str:
    value = language.strip()
    if value not in PLAYBOOK_LANGUAGES:
        raise _fail(f"Неизвестный язык playbook: {value}")
    return value


def validate_artifact_kind(kind: str) -> str:
    value = kind.strip()
    if value not in ARTIFACT_KINDS:
        raise _fail(f"Неизвестный вид артефакта: {value}")
    return value


def kinds_from_legacy(kind: str) -> list[str]:
    """The v4 kinds of an old `connections.kind` (a document behind a link is `doc` + documents)."""
    value = kind.strip()
    if value == "document":
        return ["file", "documents"]
    if value in NATURE_KINDS or value in DATA_KINDS:
        return [value]
    if value in LEGACY_KIND_ALIASES:
        return [LEGACY_KIND_ALIASES[value]]
    return ["other"]
