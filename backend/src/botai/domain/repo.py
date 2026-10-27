"""Owner: W1. Tenant-scoped reads of the domain. `user_id` is always the first argument (NFR-001).

W5's chat tools and W1's routers use these; W3's normalizer writes the tables directly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from botai.domain.schemas import (
    CourseOut,
    DeadlineOut,
    GradeOut,
    NewsOut,
    PersonOut,
    ScheduleEventOut,
)


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
