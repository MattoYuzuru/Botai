"""Owner: W1. The domain API with the tenant predicate on every query.

Reads of the synced data, plus the student's corrections of task statuses (D-045): a mark on one
task and the status rules (`domain.overrides`)."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from botai.api.deps import ClockDep
from botai.auth.deps import AuthUser
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Source, StatusRule
from botai.db.session import DbSession
from botai.domain import overrides, repo
from botai.domain.schemas import (
    CourseOut,
    DeadlineOut,
    DeadlinePatchIn,
    GradeOut,
    NewsOut,
    PersonOut,
    ProfileFieldOut,
    RankingEntryOut,
    ScheduleEventOut,
    StatusRuleIn,
    StatusRuleOut,
    TodayOut,
    UpdateOut,
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


@router.get("/profile", response_model=list[ProfileFieldOut])
async def profile(
    user: AuthUser, session: DbSession, clock: ClockDep, space_id: str | None = None
) -> list[ProfileFieldOut]:
    """The fields of the student's card that the university's sources show."""
    return await repo.list_profile(session, user.id, space_id=space_id, now=clock.now())


@router.get("/ranking", response_model=list[RankingEntryOut])
async def ranking(
    user: AuthUser,
    session: DbSession,
    clock: ClockDep,
    space_id: str | None = None,
    board: str | None = None,
) -> list[RankingEntryOut]:
    """Places of the rating boards (all of them, or one `board`)."""
    return await repo.list_ranking(
        session, user.id, space_id=space_id, board=board, now=clock.now()
    )


# --- «Новое» and the student's corrections (docs/v4/27 §4, §5.5) -------------------------------

MAX_UPDATE_DAYS = 60


@router.get("/updates", response_model=list[UpdateOut])
async def updates(
    user: AuthUser,
    session: DbSession,
    clock: ClockDep,
    space_id: str | None = None,
    days: int = Query(default=7, ge=1, le=MAX_UPDATE_DAYS),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[UpdateOut]:
    """Grades that appeared or changed and tasks whose state changed in the last `days`."""
    now = clock.now()
    return await repo.list_updates(
        session,
        user.id,
        space_id=space_id,
        since=now - timedelta(days=days),
        limit=limit,
        now=now,
    )


@router.patch("/deadlines/{deadline_id}", response_model=DeadlineOut)
async def patch_deadline(
    deadline_id: str, body: DeadlinePatchIn, user: AuthUser, session: DbSession, clock: ClockDep
) -> DeadlineOut:
    """The student's mark on one task (`open`, `submitted`, `graded`); null clears it."""
    now = clock.now()
    await overrides.set_user_state(session, user.id, deadline_id, body.user_state)
    out = await repo.get_deadline(session, user.id, deadline_id, now=now)
    await session.commit()
    return out


async def _rule_outs(
    session: DbSession, user_id: str, rules: list[StatusRule]
) -> list[StatusRuleOut]:
    wanted = sorted({rule.source_id for rule in rules if rule.source_id})
    titles: dict[str, str] = {}
    if wanted:
        rows = await session.execute(
            select(Source.id, Source.title).where(Source.user_id == user_id, Source.id.in_(wanted))
        )
        titles = {source_id: title for source_id, title in rows.all()}
    return [
        StatusRuleOut(
            id=rule.id,
            source_id=rule.source_id,
            source_title=titles.get(rule.source_id) if rule.source_id else None,
            label=rule.label,
            state=rule.state,  # pyright: ignore[reportArgumentType]  # CHECK constraint
            created_at=rule.created_at,
        )
        for rule in rules
    ]


@router.get("/status-rules", response_model=list[StatusRuleOut])
async def list_status_rules(user: AuthUser, session: DbSession) -> list[StatusRuleOut]:
    """The student's status rules, newest first."""
    return await _rule_outs(session, user.id, await overrides.list_status_rules(session, user.id))


@router.post("/status-rules", response_model=StatusRuleOut, status_code=201)
async def create_status_rule(
    body: StatusRuleIn, user: AuthUser, session: DbSession
) -> StatusRuleOut:
    """Create (or replace) the rule for a status of one source, or of every source; the stored
    tasks with that status are recomputed at once."""
    rule = await overrides.set_status_rule(
        session, user.id, source_id=body.source_id, label=body.label, state=body.state
    )
    await session.refresh(rule)
    (out,) = await _rule_outs(session, user.id, [rule])
    await session.commit()
    return out


@router.delete("/status-rules/{rule_id}", status_code=204)
async def delete_status_rule(rule_id: str, user: AuthUser, session: DbSession) -> Response:
    """Remove a rule; its tasks fall back to another rule or to the source's words."""
    await overrides.delete_status_rule(session, user.id, rule_id)
    await session.commit()
    return Response(status_code=204)
