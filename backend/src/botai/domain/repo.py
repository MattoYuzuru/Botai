"""Owner: W1. Tenant-scoped reads of the domain. `user_id` is always the first argument (NFR-001).

W5's chat tools and W1's routers use these; W3's normalizer writes the tables directly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.db.models import (
    Assignment,
    Connection,
    Course,
    Deadline,
    Document,
    Grade,
    Location,
    NewsItem,
    Person,
    ScheduleEvent,
)
from botai.domain.schemas import (
    CourseOut,
    DeadlineOut,
    GradeOut,
    NewsOut,
    PersonOut,
    ScheduleEventOut,
)

# Section kinds of the API (FR-014) and the tables that feed them, in display order.
_SECTION_TABLES = (
    ("courses", Course),
    ("scheduleEvents", ScheduleEvent),
    ("deadlines", Deadline),
    ("assignments", Assignment),
    ("grades", Grade),
    ("people", Person),
    ("locations", Location),
    ("news", NewsItem),
)


async def section_counts(
    session: AsyncSession, user_id: str, space_id: str
) -> list[tuple[str, int]]:
    """How many rows of each kind the connections of one space have (documents: the space)."""
    connection_ids = select(Connection.id).where(
        Connection.user_id == user_id, Connection.space_id == space_id
    )
    counts: list[tuple[str, int]] = []
    for kind, model in _SECTION_TABLES:
        n = await session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.user_id == user_id, model.connection_id.in_(connection_ids))
        )
        counts.append((kind, int(n or 0)))
    docs = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.user_id == user_id, Document.space_id == space_id)
    )
    counts.append(("documents", int(docs or 0)))
    return counts


async def list_events(
    session: AsyncSession,
    user_id: str,
    *,
    start: datetime,
    end: datetime,
    space_id: str | None = None,
) -> list[ScheduleEventOut]:
    raise NotImplementedError("W1")


async def list_deadlines(
    session: AsyncSession, user_id: str, *, space_id: str | None = None, include_done: bool = False
) -> list[DeadlineOut]:
    raise NotImplementedError("W1")


async def list_courses(
    session: AsyncSession, user_id: str, *, space_id: str | None = None
) -> list[CourseOut]:
    raise NotImplementedError("W1")


async def list_grades(
    session: AsyncSession, user_id: str, *, space_id: str | None = None
) -> list[GradeOut]:
    raise NotImplementedError("W1")


async def list_people(
    session: AsyncSession, user_id: str, *, space_id: str | None = None
) -> list[PersonOut]:
    raise NotImplementedError("W1")


async def list_news(
    session: AsyncSession, user_id: str, *, space_id: str | None = None
) -> list[NewsOut]:
    raise NotImplementedError("W1")
