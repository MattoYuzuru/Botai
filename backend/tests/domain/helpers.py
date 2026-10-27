"""Row builders for the domain tests. They write through the tables, like W3's normalizer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from botai.core.ids import new_id
from botai.db.models import (
    Course,
    Deadline,
    Grade,
    NewsItem,
    Person,
    ScheduleEvent,
)
from tests.auth.support import Sm

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # the frozen clock of the tests


def at(hour: int, minute: int = 0, *, day: int = 27) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def _common(user_id: str, conn_id: str, ext: str | None) -> dict[str, Any]:
    return {
        "id": new_id(),
        "user_id": user_id,
        "connection_id": conn_id,
        "external_id": ext or new_id(),
        "source_url": "https://source.example.org/x",
        "fetched_at": T0,
        "seen_at": T0,
    }


async def add(sm: Sm, *rows: Any) -> None:
    async with sm() as s:
        s.add_all(rows)
        await s.commit()


def event(
    user_id: str,
    conn_id: str,
    starts_at: datetime,
    ends_at: datetime | None = None,
    *,
    title: str = "Пара",
    ext: str | None = None,
    **kw: Any,
) -> ScheduleEvent:
    return ScheduleEvent(
        title=title,
        starts_at=starts_at,
        ends_at=ends_at or starts_at + timedelta(minutes=80),
        **_common(user_id, conn_id, ext),
        **kw,
    )


def deadline(
    user_id: str,
    conn_id: str,
    due_at: datetime | None,
    *,
    title: str = "Сдать работу",
    state: str = "open",
    effective_due_at: datetime | None = None,
    **kw: Any,
) -> Deadline:
    return Deadline(
        title=title,
        due_at=due_at,
        effective_due_at=effective_due_at,
        state=state,
        **_common(user_id, conn_id, kw.pop("ext", None)),
        **kw,
    )


def course(user_id: str, conn_id: str, title: str = "Матан", **kw: Any) -> Course:
    ext = kw.pop("ext", None)
    return Course(title=title, **_common(user_id, conn_id, ext), **kw)


def grade(user_id: str, conn_id: str, value: str = "8", **kw: Any) -> Grade:
    ext = kw.pop("ext", None)
    return Grade(value=value, **_common(user_id, conn_id, ext), **kw)


def person(user_id: str, conn_id: str, name: str = "Иванов И. И.", **kw: Any) -> Person:
    ext = kw.pop("ext", None)
    return Person(name=name, **_common(user_id, conn_id, ext), **kw)


def news(user_id: str, conn_id: str, published_at: datetime, title: str = "Новость") -> NewsItem:
    return NewsItem(title=title, published_at=published_at, **_common(user_id, conn_id, None))


@dataclass(frozen=True, slots=True)
class World:
    """A signed-in student with two universities, one active connection in each."""

    client: httpx.AsyncClient
    user_id: str
    space_a: str
    space_b: str
    conn_a: str
    conn_b: str
