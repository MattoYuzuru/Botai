"""The v4 fields of grades and tasks (D-043): layers, a separate maximum, the source's status
words. D-039 holds: the application never averages or converts a scale."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import pytest

from botai.sources.grade_scale import format_number, grade_numbers
from botai.sources.normalize import coerce_fields, task_status
from botai.sources.vocab import ALL_VOCAB, VOCAB

MSK = ZoneInfo("Europe/Moscow")
SRC = "https://uni.example/api/tasks"
DUE = {"title": "ДЗ", "dueAt": "2026-11-10T20:59:00Z"}


def grade(**fields: Any) -> dict[str, Any]:
    row = coerce_fields("grades", fields, SRC, MSK)
    assert row is not None
    return row


def test_the_v4_fields_are_v4_only() -> None:
    assert {"layer", "max", "period", "assignmentId", "url", "comment"} <= ALL_VOCAB["grades"]
    assert "stateText" in ALL_VOCAB["deadlines"]
    assert "layer" not in VOCAB["grades"]  # the v3 recipe language stays as it was
    assert "stateText" not in VOCAB["deadlines"]


def test_a_separate_maximum_gives_numbers_and_a_readable_scale() -> None:
    row = grade(value=8, max=10, layer="item", assignmentId=8001, url="/tasks/8001")
    assert (row["value"], row["scale"], row["value_num"], row["scale_max"]) == (
        "8",
        "из 10",
        8.0,
        10.0,
    )
    assert (row["layer"], row["assignment_external_id"]) == ("item", "8001")
    assert row["url"] == "https://uni.example/tasks/8001"
    assert grade(value="7,5", max="7.5")["scale"] == "из 7,5"
    # a scale text without a number keeps its words, the maximum gives the top
    row = grade(value="11", scale="баллы за курс", max=20)
    assert (row["scale"], row["value_num"], row["scale_max"]) == ("баллы за курс", 11.0, 20.0)


@pytest.mark.parametrize(
    ("value", "scale", "top", "expected"),
    [
        ("8", "10", 10.0, (8.0, 10.0)),
        ("8", "из 10", 5.0, (None, None)),  # the text and the number disagree: no numbers
        ("8/10", None, 100.0, (None, None)),
        ("86%", None, 100.0, (86.0, 100.0)),
        ("12", None, 10.0, (None, None)),  # above its own scale
        ("зачтено", None, 1.0, (None, None)),
        ("8", None, None, (None, None)),
    ],
)
def test_every_statement_of_the_scale_must_agree(
    value: str, scale: str | None, top: float | None, expected: tuple[float | None, float | None]
) -> None:
    assert grade_numbers(value, scale, top) == expected


def test_layer_period_comment_and_bad_values() -> None:
    row = grade(value="A", layer=" Final ", period="2025/26, 1 семестр", comment="  Хорошо  ")
    assert (row["layer"], row["period"], row["comment"]) == (
        "final",
        "2025/26, 1 семестр",
        "Хорошо",
    )
    assert grade(value="5", layer="exam")["layer"] is None  # outside the vocabulary
    assert grade(value="5", url="javascript:alert(1)")["url"] is None
    assert grade(value="5", max=0)["scale_max"] is None
    assert grade(value="5", max=True)["scale"] is None
    assert len(grade(value="5", comment="x" * 5000)["comment"]) == 2000


def test_format_number() -> None:
    assert [format_number(n) for n in (10.0, 7.5, 0.25, 100.0)] == ["10", "7,5", "0,25", "100"]


@pytest.mark.parametrize(
    ("state", "state_text", "expected"),
    [
        ("submitted", "Решение прикреплено", ("submitted", "Решение прикреплено")),
        (" Graded ", None, ("graded", None)),
        ("unknown", "Сдано", ("submitted", "Сдано")),  # `unknown` says nothing: the words decide
        ("unknown", None, ("unknown", None)),
        ("Отправлено", None, ("submitted", "Отправлено")),
        (None, "На проверке", ("submitted", "На проверке")),
        ("review", "Ждёт", ("submitted", "Ждёт")),  # words unknown: the machine word decides
        ("backlog", None, ("open", "backlog")),
        (None, None, ("unknown", None)),
        (3, "—", ("unknown", None)),
    ],
)
def test_task_status_keeps_the_words_and_reads_the_state(
    state: object, state_text: object, expected: tuple[str, str | None]
) -> None:
    assert task_status(state, state_text) == expected
    row = coerce_fields("deadlines", {**DUE, "state": state, "stateText": state_text}, SRC, MSK)
    assert row is not None
    assert (row["state"], row["state_text"]) == expected
