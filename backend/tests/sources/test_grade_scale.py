"""The number and the top of a grade's scale: set only when the source printed both plainly (D-039).

The application never averages, converts between scales or guesses: everything unclear is
`(None, None)`, and the grade is still stored and shown as the source printed it.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import pytest

from botai.sources import normalize
from botai.sources.adapter import Record
from botai.sources.grade_scale import grade_numbers, plain_number

TZ = ZoneInfo("Europe/Moscow")

NUMBERS: list[tuple[Any, Any, tuple[float | None, float | None]]] = [
    # a plain number and the top of the scale in the usual spellings
    ("8", "10", (8.0, 10.0)),
    ("8", "из 10", (8.0, 10.0)),
    ("8", "10 баллов", (8.0, 10.0)),
    ("8", "из 10 баллов", (8.0, 10.0)),
    ("8", "10-балльная", (8.0, 10.0)),
    ("4", "5-балльная шкала", (4.0, 5.0)),
    ("8", "шкала 10", (8.0, 10.0)),
    ("8", "0-10", (8.0, 10.0)),
    ("8", "0–10 баллов", (8.0, 10.0)),
    ("8", "от 0 до 10", (8.0, 10.0)),
    ("8", "/10", (8.0, 10.0)),
    ("92", "100", (92.0, 100.0)),
    ("92", "100 points", (92.0, 100.0)),
    # decimals with a comma or a dot, numbers that arrive as numbers
    ("8,5", "10", (8.5, 10.0)),
    ("8.5", "10", (8.5, 10.0)),
    (8.5, "10", (8.5, 10.0)),
    (8, 10, (8.0, 10.0)),
    (0, "10", (0.0, 10.0)),
    ("0", "10", (0.0, 10.0)),
    ("10", "10", (10.0, 10.0)),
    # the value carries its own scale
    ("8/10", None, (8.0, 10.0)),
    ("8 из 10", None, (8.0, 10.0)),
    ("8,5 из 10", "10 баллов", (8.5, 10.0)),
    ("86%", None, (86.0, 100.0)),
    ("86 %", "100 баллов", (86.0, 100.0)),
    # not unambiguous: words, a number without a scale, a number above the scale, scales that differ
    ("зачтено", "зачёт/незачёт", (None, None)),
    ("отлично", "5-балльная", (None, None)),
    ("B+", "A-F", (None, None)),
    ("8", None, (None, None)),
    ("8", "", (None, None)),
    ("8", "по усмотрению", (None, None)),
    ("11", "10", (None, None)),
    ("-1", "10", (None, None)),
    ("8", "0", (None, None)),
    ("8/10", "100", (None, None)),
    ("86%", "10", (None, None)),
    ("8 или 9", "10", (None, None)),
    ("1 000", "10000", (None, None)),
    ("8e1", "100", (None, None)),
    ("nan", "10", (None, None)),
    (float("nan"), "10", (None, None)),
    (float("inf"), "10", (None, None)),
    (True, "10", (None, None)),
    (None, "10", (None, None)),
    ("", "10", (None, None)),
]


@pytest.mark.parametrize(("value", "scale", "expected"), NUMBERS)
def test_a_grade_has_numbers_only_when_the_source_gave_both(
    value: Any, scale: Any, expected: tuple[float | None, float | None]
) -> None:
    assert grade_numbers(value, scale) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("92,5", 92.5),
        ("92.5", 92.5),
        (" 7 ", 7.0),
        (7, 7.0),
        (7.5, 7.5),
        ("зачтено", None),
        ("7 баллов", None),
        ("86%", None),
        ("", None),
        (None, None),
        (False, None),
        (float("nan"), None),
    ],
)
def test_plain_number_reads_only_a_number_on_its_own(raw: Any, expected: float | None) -> None:
    assert plain_number(raw) == expected


GRADE_ROWS: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
    (
        {"title": "Коллоквиум", "value": "8", "scale": "10 баллов"},
        {"title": "Коллоквиум", "value": "8", "scale": "10 баллов", "value_num": 8.0},
    ),
    (
        {"value": "8,50", "scale": "из 10"},
        {"value": "8,50", "scale": "из 10", "value_num": 8.5, "scale_max": 10.0},
    ),
    (
        {"value": "зачтено", "scale": "зачёт/незачёт"},
        {"value": "зачтено", "value_num": None, "scale_max": None},
    ),
    ({"value": "7"}, {"value": "7", "scale": None, "value_num": None, "scale_max": None}),
    # a number as a number: a whole float is printed without ".0", as before
    ({"value": 9.0, "scale": 10}, {"value": "9", "scale": "10", "value_num": 9.0}),
    # "no value" spelled out never becomes the text "None": the record is dropped
    ({"value": "None", "scale": "10"}, None),
    ({"value": "null"}, None),
    ({"value": "  "}, None),
    ({"value": "—"}, None),
    ({"value": None, "scale": "10"}, None),
    # a placeholder around a real value is just absent
    (
        {"title": "None", "value": "9", "scale": "None"},
        {"title": None, "value": "9", "scale": None, "value_num": None},
    ),
]


@pytest.mark.parametrize(("fields", "expected"), GRADE_ROWS)
def test_the_grade_coercer_fills_numbers_and_drops_placeholders(
    fields: dict[str, Any], expected: dict[str, Any] | None
) -> None:
    record = Record(
        kind="grades", external_id="g", fields=fields, source_url="https://fakeuni.test"
    )
    row = normalize.coerce("grades", record, TZ)
    if expected is None:
        assert row is None
        return
    assert row is not None
    for column, value in expected.items():
        assert row[column] == value, column
    assert "None" not in {row["value"], row["title"], row["scale"]}


def test_a_coerced_grade_is_never_computed_from_other_grades() -> None:
    """Two grades of one course give two rows: nothing is averaged or merged."""
    rows = [
        normalize.coerce(
            "grades",
            Record(kind="grades", external_id=name, fields=fields, source_url="https://x.test"),
            TZ,
        )
        for name, fields in (
            ("a", {"value": "8", "scale": "10"}),
            ("b", {"value": "6", "scale": "10"}),
        )
    ]
    assert [row["value_num"] for row in rows if row] == [8.0, 6.0]
