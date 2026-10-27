"""Read access to the student's domain data for chat tools.

The production implementation is `botai.domain.repo` (W1: `user_id` is the first argument, tenant
predicate on every query). The protocol keeps the chat tools testable before and after it lands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from botai.domain import repo
from botai.domain.schemas import (
    CourseOut,
    DeadlineOut,
    GradeOut,
    NewsOut,
    PersonOut,
    ScheduleEventOut,
)


class DomainReader(Protocol):
    async def events(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        start: datetime,
        end: datetime,
        space_id: str | None,
    ) -> list[ScheduleEventOut]: ...

    async def deadlines(
        self, session: AsyncSession, user_id: str, *, space_id: str | None, include_done: bool
    ) -> list[DeadlineOut]: ...

    async def courses(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[CourseOut]: ...

    async def grades(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[GradeOut]: ...

    async def people(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[PersonOut]: ...

    async def news(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[NewsOut]: ...


class RepoReader:
    """Delegates to W1's tenant-scoped repository."""

    async def events(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        start: datetime,
        end: datetime,
        space_id: str | None,
    ) -> list[ScheduleEventOut]:
        return await repo.list_events(session, user_id, start=start, end=end, space_id=space_id)

    async def deadlines(
        self, session: AsyncSession, user_id: str, *, space_id: str | None, include_done: bool
    ) -> list[DeadlineOut]:
        return await repo.list_deadlines(
            session, user_id, space_id=space_id, include_done=include_done
        )

    async def courses(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[CourseOut]:
        return await repo.list_courses(session, user_id, space_id=space_id)

    async def grades(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[GradeOut]:
        return await repo.list_grades(session, user_id, space_id=space_id)

    async def people(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[PersonOut]:
        return await repo.list_people(session, user_id, space_id=space_id)

    async def news(
        self, session: AsyncSession, user_id: str, *, space_id: str | None
    ) -> list[NewsOut]:
        return await repo.list_news(session, user_id, space_id=space_id)
