"""Owner: W1. Read-only domain API with tenant predicate on every query."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from botai.core.errors import not_implemented
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


@router.get("/today", response_model=TodayOut)
async def today() -> TodayOut:
    raise not_implemented("today")


@router.get("/schedule", response_model=list[ScheduleEventOut])
async def schedule(
    start: datetime, end: datetime, space_id: str | None = None
) -> list[ScheduleEventOut]:
    raise not_implemented("schedule")


@router.get("/deadlines", response_model=list[DeadlineOut])
async def deadlines(space_id: str | None = None, include_done: bool = False) -> list[DeadlineOut]:
    raise not_implemented("deadlines")


@router.get("/courses", response_model=list[CourseOut])
async def courses(space_id: str | None = None) -> list[CourseOut]:
    raise not_implemented("courses")


@router.get("/grades", response_model=list[GradeOut])
async def grades(space_id: str | None = None) -> list[GradeOut]:
    raise not_implemented("grades")


@router.get("/people", response_model=list[PersonOut])
async def people(space_id: str | None = None) -> list[PersonOut]:
    raise not_implemented("people")


@router.get("/news", response_model=list[NewsOut])
async def news(space_id: str | None = None) -> list[NewsOut]:
    raise not_implemented("news")
