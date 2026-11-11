"""Natural keys of domain rows: how the reads recognise one entity across the sources of a
university (the dedupe of `domain.repo`) and how a student's mark reaches every copy of a task
(`domain.overrides`). One definition, so the two can never disagree."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def aware(value: datetime) -> datetime:
    """A naive datetime from a caller is taken as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def moment(value: datetime | None) -> str:
    """The comparable natural key of a moment: the same instant is the same string."""
    return aware(value).astimezone(UTC).isoformat() if value is not None else ""


def text_key(value: str | None) -> str:
    """The comparable natural key of a human text: casefolded, ё as е, spacing collapsed."""
    return " ".join((value or "").casefold().replace("ё", "е").split())


def deadline_key(row: Any, space_id: str) -> tuple[str, str, str]:
    """One task of one university: its space, its title and its due moment."""
    return (space_id, text_key(row.title), moment(row.effective_due_at or row.due_at))
