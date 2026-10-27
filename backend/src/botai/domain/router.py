"""Owner: W1. Read-only domain API with tenant predicate on every query."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter

from botai.api.deps import ClockDep
from botai.auth.deps import AuthUser
from botai.core.errors import BotaiError, ErrorCode
from botai.db.session import DbSession
from botai.domain import repo
from botai.domain.schemas import (
    CourseOut,
    DeadlineOut,
    GradeOut,
    NewsOut,
    PersonOut,
    ScheduleEventOut,
    TodayOut,
)

router = APIRouter(prefix="/api/v1", tags=["domain"])

MAX_SCHEDULE_RANGE = timedelta(days=400)


@router.get("/today", response_model=TodayOut)
async def today(user: AuthUser, session: DbSession, clock: ClockDep) -> TodayOut:
    return await repo.today(session, user.id, now=clock.now())


@router.get("/schedule", response_model=list[ScheduleEventOut])
async def schedule(
    start: datetime,
    end: datetime,
    user: AuthUser,
    session: DbSession,
    clock: ClockDep,
    space_id: str | None = None,
) -> list[ScheduleEventOut]:
    """Classes overlapping [start, end) from all spaces (or one), with cross-space conflicts.
    A datetime without an offset is read as UTC."""
    if end <= start:
        raise BotaiError(ErrorCode.VALIDATION, "Конец периода должен быть позже начала")
    if end - start > MAX_SCHEDULE_RANGE:
        raise BotaiError(ErrorCode.VALIDATION, "Период слишком длинный")
    return await repo.list_events(
        session, user.id, start=start, end=end, space_id=space_id, now=clock.now()
    )


@router.get("/deadlines", response_model=list[DeadlineOut])
async def deadlines(
    user: AuthUser,
    session: DbSession,
    clock: ClockDep,
    space_id: str | None = None,
    include_done: bool = False,
) -> list[DeadlineOut]:
    return await repo.list_deadlines(
        session, user.id, space_id=space_id, include_done=include_done, now=clock.now()
    )


@router.get("/courses", response_model=list[CourseOut])
async def courses(
    user: AuthUser, session: DbSession, clock: ClockDep, space_id: str | None = None
) -> list[CourseOut]:
    return await repo.list_courses(session, user.id, space_id=space_id, now=clock.now())


@router.get("/grades", response_model=list[GradeOut])
async def grades(
    user: AuthUser, session: DbSession, clock: ClockDep, space_id: str | None = None
) -> list[GradeOut]:
    return await repo.list_grades(session, user.id, space_id=space_id, now=clock.now())


@router.get("/people", response_model=list[PersonOut])
async def people(
    user: AuthUser, session: DbSession, clock: ClockDep, space_id: str | None = None
) -> list[PersonOut]:
    return await repo.list_people(session, user.id, space_id=space_id, now=clock.now())


@router.get("/news", response_model=list[NewsOut])
async def news(
    user: AuthUser, session: DbSession, clock: ClockDep, space_id: str | None = None
) -> list[NewsOut]:
    return await repo.list_news(session, user.id, space_id=space_id, now=clock.now())
