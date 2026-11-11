"""Panels never mix layers of grades (D-043): a chart and the best grade read works only."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from botai.domain.schemas import GradeOut, Provenance
from botai.panels.data import grades_data
from botai.panels.schemas import ChartSpec, GradesData, KpiSpec, parse_spec

GRADES = {"kind": "grades"}
PROV = Provenance(
    source_id="src",
    space_id="space",
    source_url="https://uni.example/api",
    fetched_at=datetime(2026, 11, 1, tzinfo=UTC),
    stale=False,
)


def grade(title: str, value: float, top: float, day: int, layer: str | None) -> GradeOut:
    return GradeOut(
        id=title,
        title=title,
        value=f"{value:g}",
        scale=f"из {top:g}",
        weight=None,
        course_title=None,
        graded_at=datetime(2026, 10, day, tzinfo=UTC),
        provenance=PROV,
        value_num=value,
        scale_max=top,
        layer=layer,  # pyright: ignore[reportArgumentType]
    )


def spec(raw: dict[str, Any]) -> Any:
    return parse_spec({"v": 1, "title": "Оценки", "icon": "grade", "dataset": GRADES, **raw})


# newest first, like `repo.list_grades`
DATASET = [
    grade("Итог курса", 10, 10, 30, "final"),
    grade("Накопленная", 9.5, 10, 29, "current"),
    grade("ДЗ 2", 7, 10, 20, "item"),
    grade("ДЗ 1", 8, 10, 10, None),
]


def test_best_and_chart_points_read_grades_of_works_only() -> None:
    kpi = spec({"type": "kpi", "view": {"metrics": ["count", "latest", "best"]}})
    assert isinstance(kpi, KpiSpec)
    data = grades_data(DATASET, kpi)
    assert isinstance(data, GradesData)
    assert (data.total, data.numeric) == (4, 4)  # every grade is still counted
    assert data.latest is not None
    assert data.latest.title == "Итог курса"
    assert data.best is not None
    assert data.best.title == "ДЗ 1"
    chart = spec({"type": "chart", "view": {"variant": "line", "x": "date", "limit": 50}})
    assert isinstance(chart, ChartSpec)
    points = grades_data(DATASET, chart)
    assert isinstance(points, GradesData)
    assert [(p.title, p.layer) for p in points.points] == [("ДЗ 1", None), ("ДЗ 2", "item")]
