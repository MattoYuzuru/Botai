"""Resolving a panel's dataset into bounded rows (D-039).

Reads go through the tenant-scoped domain repositories, so a panel sees exactly what the
section screens see: live sources only, deduplicated by the source priorities. Nothing is
computed from the student's data: `latest` and `best` pick one grade out of the dataset
(`best` only inside one scale), numbers appear only where the source gave them, and every list is
cut by the limit of the panel's view. Layers are never mixed on a chart or in `best`: they read the
grades of works (layer `item`, or none given); a course total, a final mark or a GPA would not be
comparable with them (D-043).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError
from botai.db.models import Panel
from botai.domain import repo
from botai.domain.schemas import GradeOut, ProfileFieldOut, RankingEntryOut
from botai.panels.schemas import (
    MAX_CHART_SERIES,
    ChartSpec,
    GradePointOut,
    GradesData,
    KpiSpec,
    ListSpec,
    PanelData,
    PanelSpecModel,
    ProfileData,
    ProfileFieldData,
    ProfileSpec,
    RankingData,
    RankingRowData,
    RankingSpec,
    TableSpec,
    parse_spec,
)

MAX_PROFILE_ROWS = 60


def _point(grade: GradeOut) -> GradePointOut:
    return GradePointOut(
        id=grade.id,
        title=grade.title,
        course=grade.course_title,
        course_id=grade.course_id,
        value=grade.value,
        scale=grade.scale,
        value_num=grade.value_num,
        scale_max=grade.scale_max,
        graded_at=grade.graded_at,
        layer=grade.layer,
    )


def _is_item(grade: GradeOut) -> bool:
    """A grade of one work: what a chart and the best grade compare (no layer means an item)."""
    return grade.layer in (None, "item")


def _is_numeric(grade: GradeOut) -> bool:
    return grade.value_num is not None and grade.scale_max is not None


def _best(grades: list[GradeOut]) -> GradeOut | None:
    """The highest grade of the scale that most grades use: grades of different scales are never
    compared with each other."""
    numeric = [g for g in grades if _is_numeric(g) and _is_item(g)]
    if not numeric:
        return None
    sizes = Counter(g.scale_max for g in numeric)
    scale = max(sizes, key=lambda top: (sizes[top], top or 0))
    in_scale = [g for g in numeric if g.scale_max == scale]
    return max(in_scale, key=lambda g: (g.value_num or 0, _moment(g)))


def _moment(grade: GradeOut) -> float:
    return grade.graded_at.timestamp() if grade.graded_at else float("-inf")


def _chart_points(grades: list[GradeOut], spec: ChartSpec) -> list[GradeOut]:
    """Numeric grades of at most `MAX_CHART_SERIES` scales (the busiest ones), the newest
    `limit` of them; by date they are oldest first, so a line reads left to right."""
    numeric = [g for g in grades if _is_numeric(g) and _is_item(g)]
    if spec.view.x == "date":
        numeric = [g for g in numeric if g.graded_at is not None]
    sizes = Counter(g.scale_max for g in numeric)
    kept = set(sorted(sizes, key=lambda top: (-sizes[top], -(top or 0)))[:MAX_CHART_SERIES])
    newest = [g for g in numeric if g.scale_max in kept][: spec.view.limit]
    if spec.view.x == "date":
        newest.sort(key=_moment)
    return newest


def grades_data(
    grades: list[GradeOut], spec: KpiSpec | ChartSpec | TableSpec | ListSpec
) -> PanelData:
    """The grades of a dataset for one view; `grades` come newest first (`repo.list_grades`)."""
    if spec.dataset.course_id is not None:
        grades = [g for g in grades if g.course_id == spec.dataset.course_id]
    if isinstance(spec, KpiSpec):
        shown: list[GradeOut] = []
    elif isinstance(spec, ChartSpec):
        shown = _chart_points(grades, spec)
    else:
        shown = grades[: spec.view.limit]
    best = _best(grades)
    return GradesData(
        total=len(grades),
        numeric=sum(1 for g in grades if _is_numeric(g)),
        points=[_point(g) for g in shown],
        latest=_point(grades[0]) if grades else None,
        best=_point(best) if best else None,
    )


def profile_data(fields: list[ProfileFieldOut], spec: ProfileSpec) -> PanelData:
    wanted = spec.view.fields
    if wanted:
        by_key = {item.field.casefold(): item for item in fields}
        chosen = [by_key[key.casefold()] for key in wanted if key.casefold() in by_key]
    else:
        chosen = fields[:MAX_PROFILE_ROWS]
    return ProfileData(
        total=len(fields),
        fields=[
            ProfileFieldData(field=f.field, label=f.label, value=f.value, group=f.group)
            for f in chosen
        ],
    )


def ranking_data(entries: list[RankingEntryOut], spec: RankingSpec) -> PanelData:
    """The top of a board, plus the student's own place when it lies below the top."""
    ordered = sorted(entries, key=lambda e: (e.position, e.label))
    top = ordered[: spec.view.top]
    if spec.view.highlight_me and not any(e.is_me for e in top):
        mine = next((e for e in ordered if e.is_me), None)
        if mine is not None:
            top.append(mine)
    return RankingData(
        board=spec.dataset.board or "",
        total=len(ordered),
        participants=next((e.total for e in ordered if e.total is not None), None),
        period=next((e.period for e in ordered if e.period), None),
        rows=[
            RankingRowData(
                position=e.position,
                label=e.label,
                score=e.score,
                score_num=e.score_num,
                is_me=e.is_me,
                total=e.total,
                period=e.period,
            )
            for e in top
        ],
    )


class DataResolver:
    """Resolves the data of the panels of one request; repeated reads of a dataset are shared."""

    def __init__(self, session: AsyncSession, user_id: str, now: datetime) -> None:
        self._session, self._user_id, self._now = session, user_id, now
        self._grades: dict[str, list[GradeOut]] = {}
        self._profile: dict[str, list[ProfileFieldOut]] = {}
        self._ranking: dict[tuple[str, str], list[RankingEntryOut]] = {}

    async def resolve(self, panel: Panel) -> PanelData | None:
        """The data of a panel, or None when its stored spec no longer validates."""
        try:
            spec: PanelSpecModel = parse_spec(panel.spec)
        except BotaiError:
            return None
        space_id = panel.space_id
        if isinstance(spec, ProfileSpec):
            if space_id not in self._profile:
                self._profile[space_id] = await repo.list_profile(
                    self._session, self._user_id, space_id=space_id, now=self._now
                )
            return profile_data(self._profile[space_id], spec)
        if isinstance(spec, RankingSpec):
            key = (space_id, spec.dataset.board or "")
            if key not in self._ranking:
                self._ranking[key] = await repo.list_ranking(
                    self._session,
                    self._user_id,
                    space_id=space_id,
                    board=spec.dataset.board,
                    now=self._now,
                )
            return ranking_data(self._ranking[key], spec)
        if space_id not in self._grades:
            self._grades[space_id] = await repo.list_grades(
                self._session, self._user_id, space_id=space_id, now=self._now
            )
        return grades_data(self._grades[space_id], spec)
