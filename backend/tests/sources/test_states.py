"""The status words of learning systems → the closed set of task states (D-043 §3.3)."""

from __future__ import annotations

import pytest

from botai.sources.states import is_status, task_state


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # handed in: nothing is needed from the student
        ("Решение прикреплено", "submitted"),
        ("Отправлено", "submitted"),
        ("Файл загружен", "submitted"),
        ("Сдано", "submitted"),
        ("На проверке", "submitted"),
        ("Ожидает проверки", "submitted"),
        ("uploaded", "submitted"),
        # checked
        ("Принято", "graded"),
        ("Работа принята", "graded"),
        ("Решение принято", "graded"),
        ("Оценка выставлена", "graded"),
        ("Балл выставлен", "graded"),
        ("Оценено", "graded"),
        ("Проверено", "graded"),
        ("Graded and returned", "graded"),  # Classroom returns graded work
        ("Оценено и возвращено", "graded"),
        # `выставлен…` and `принят…` without a grade or a work next to them say nothing
        ("Задание выставлено", "unknown"),
        ("Выставлен срок сдачи", "unknown"),
        ("Принят к сведению", "unknown"),
        ("Заявка принята", "unknown"),
        ("RETURNED", "unknown"),
        ("Возвращено", "unknown"),
        # the student did the rework and handed it in again
        ("Доработано", "submitted"),
        ("Работа доработана и отправлена", "submitted"),
        ("Отправлено после доработки", "submitted"),
        # still to do
        ("Задано", "open"),
        ("Задание выдано", "unknown"),  # `задание` is a noun, not `задано`
        ("Задание сдано", "submitted"),
        ("В работе", "open"),
        ("Не начато", "open"),
        ("notStarted", "open"),
        ("На доработке", "open"),
        ("Требует доработки", "open"),
        ("Отправлено на доработку", "open"),
        ("Возвращено на доработку", "open"),
        ("Проверено, нужна доработка", "open"),
        ("needs revision", "open"),
        ("Черновик", "open"),
        ("draft", "open"),
        ("rework", "open"),
        ("Не сдано", "open"),
        ("Не отправлено", "open"),
        ("Не принято", "open"),
        # missed
        ("Просрочено", "missed"),
        # what the vocabulary cannot tell
        ("Не оценено", "unknown"),
        ("Работа", "unknown"),
        ("", "unknown"),
    ],
)
def test_status_words_map_to_states(raw: str, expected: str) -> None:
    assert task_state(raw) == expected, raw


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("inProgress", True),
        ("awaiting-review", True),
        ("draft", True),
        ("rework", True),
        ("Ivanov", False),
        ("На проверке", False),  # Russian status filters are not machine words of a query
    ],
)
def test_is_status_keeps_its_plain_english_contract(value: str, expected: bool) -> None:
    assert is_status(value) is expected
