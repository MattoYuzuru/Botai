"""The extended status vocabulary (D-043 §3.3) against the one before it (`states_v1`).

A label that the old vocabulary read as handed in or graded must never become «to do»: that would
put finished work back on the student's list. A broad list of labels of learning systems.
"""

from __future__ import annotations

from botai.sources.states import task_state
from tests.sources import states_v1

LABELS = (
    # English machine words and phrases
    "submitted", "Submitted", "SUBMITTED", "done", "completed", "sent", "review", "inReview",
    "IN_REVIEW", "under review", "pendingEvaluation", "awaiting_evaluation", "waitingForReview",
    "graded", "evaluated", "scored", "marked", "checked", "passed", "failed", "Graded and returned",
    "RETURNED", "returned", "turned in", "TURNED_IN", "reclaimed", "resubmitted", "late",
    "submitted late", "completed late", "overdue", "missed", "backlog", "inProgress", "open",
    "new", "todo", "assigned", "draft", "notSubmitted", "notEvaluated", "NOT_GRADED", "pending",
    "accepted", "approved", "rejected", "declined", "closed", "finished", "excused",
    # Russian labels of cabinets and LMS pages
    "Сдано", "Сдана", "Сдан", "Сдано с опозданием", "Выполнено", "Выполнена", "Отправлено",
    "Отправлено на проверку", "Отправлено после доработки", "Решение прикреплено",
    "Файл загружен", "На проверке", "Ожидает проверки", "Проверяется", "Проверено", "Оценено",
    "Оценено и возвращено", "Оценка выставлена", "Зачтено", "Не зачтено", "Принято",
    "Работа принята", "Решение принято", "Доработано", "Работа доработана и отправлена",
    "Возвращено", "Просрочено", "Задано", "Назначено", "Активно", "В работе", "Не сдано",
    "Не отправлено", "Не начато", "Не оценено", "Черновик", "Задание выставлено",
    "Выставлен срок сдачи", "Принят к сведению", "Заявка принята", "Сдано, оценено",
    "Проверено преподавателем", "Работа проверена", "Работа оценена", "Сдача закрыта",
)  # fmt: skip
DONE = frozenset({"submitted", "graded"})


def test_no_handed_in_or_graded_label_becomes_open() -> None:
    regressions = {
        label: (states_v1.task_state(label), task_state(label))
        for label in LABELS
        if states_v1.task_state(label) in DONE and task_state(label) == "open"
    }
    assert regressions == {}


def test_a_known_label_never_falls_back_to_unknown() -> None:
    forgotten = {
        label: states_v1.task_state(label)
        for label in LABELS
        if states_v1.task_state(label) != "unknown" and task_state(label) == "unknown"
    }
    assert forgotten == {}
