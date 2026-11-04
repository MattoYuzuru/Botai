"""Rows the panels tests need: sources and domain rows with `source_id` provenance."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from botai.core.ids import new_id
from botai.db.models import Course, Grade, ProfileField, RankingEntry
from botai.sources.v4 import service
from tests.auth.support import Sm

BASE = "https://fakeuni.test"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def provenance(user_id: str, source_id: str, external_id: str | None = None) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "connection_id": None,
        "source_id": source_id,
        "external_id": external_id or new_id(),
        "source_url": f"{BASE}/api",
        "fetched_at": NOW,
        "seen_at": NOW,
    }


async def make_source(
    sm: Sm, *, user_id: str, space_id: str, title: str = "ЛК Fakeuni", url: str = f"{BASE}/app"
) -> str:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=user_id,
            space_id=space_id,
            title=title,
            entry_url=url,
            kinds=["site", "grades"],
        )
        await session.commit()
        return source.id


@dataclass(frozen=True, slots=True)
class G:
    """One grade row: `value_num`/`scale_max` set only when the source gave both."""

    title: str
    value: str
    scale: str | None = None
    value_num: float | None = None
    scale_max: float | None = None
    graded_at: datetime | None = None
    course: str | None = None  # external id of a course of the same source


async def add_grades(
    sm: Sm, *, user_id: str, source_id: str, grades: Sequence[G], prefix: str = "g"
) -> list[str]:
    ids: list[str] = []
    async with sm() as session:
        for i, g in enumerate(grades):
            row = Grade(
                title=g.title,
                value=g.value,
                scale=g.scale,
                value_num=g.value_num,
                scale_max=g.scale_max,
                graded_at=g.graded_at,
                course_external_id=g.course,
                **provenance(user_id, source_id, f"{prefix}-{i}"),
            )
            session.add(row)
            ids.append(row.id)
        await session.commit()
    return ids


async def add_course(sm: Sm, *, user_id: str, source_id: str, external_id: str, title: str) -> str:
    async with sm() as session:
        row = Course(title=title, archived=False, **provenance(user_id, source_id, external_id))
        session.add(row)
        await session.commit()
        return row.id


async def add_profile(
    sm: Sm,
    *,
    user_id: str,
    source_id: str,
    fields: Sequence[tuple[str, str, str, str | None]],
) -> None:
    """`(field, label, value, group)` rows of the student's card."""
    async with sm() as session:
        for field, label, value, group in fields:
            session.add(
                ProfileField(
                    field=field,
                    label=label,
                    value=value,
                    group_text=group,
                    **provenance(user_id, source_id, field),
                )
            )
        await session.commit()


async def add_ranking(
    sm: Sm,
    *,
    user_id: str,
    source_id: str,
    board: str,
    places: Sequence[tuple[int, str, str | None, bool]],
    total: int | None = 120,
    period: str | None = "Осень 2026",
) -> None:
    """`(position, label, score, is_me)` rows of one board."""
    async with sm() as session:
        for position, label, score, is_me in places:
            session.add(
                RankingEntry(
                    board=board,
                    position=position,
                    label=label,
                    score=score,
                    score_num=float(score.replace(",", ".")) if score else None,
                    is_me=is_me,
                    total=total,
                    period=period,
                    **provenance(user_id, source_id, f"{board}-{position}"),
                )
            )
        await session.commit()


def at(day: int, month: int = 10) -> datetime:
    return datetime(2026, month, day, 10, 0, tzinfo=UTC)
