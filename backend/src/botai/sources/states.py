"""The state of a task in the words of a source, as one of the states the domain knows.

A source names a state however it likes: `inProgress`, `IN_PROGRESS`, `in-progress`, `Не сдано`.
The value is split into words (case, `_`, `-`, spaces) and each word is matched whole against a
small generic vocabulary. A substring is never enough: `preview` is not `review`, `reviewed` is not
`review`, and a word that negates or postpones (`not`, `pending`, `awaiting`, `не`) is read before
the word it stands next to: `notEvaluated` is not `evaluated`.

The vocabulary is the common one of learning systems, not of one cabinet. What it does not know is
`unknown`, which keeps a task on the student's list rather than hiding it.
"""

from __future__ import annotations

import re
from typing import Literal

State = Literal["open", "submitted", "graded", "missed", "unknown"]

# English words are matched whole; Russian ones by stem, since the ending follows the noun.
_WORD = re.compile(r"[A-ZА-ЯЁ]?[a-zа-яё]+|[A-ZА-ЯЁ]+(?![a-zа-яё])")
_CYRILLIC = re.compile(r"[а-яё]+")

NEGATIONS = frozenset({"not", "no", "never", "without", "не", "нет", "без"})
WAITING = frozenset({"pending", "awaiting", "await", "waiting", "wait", "ожидает", "ожидание"})
# Being checked, or having been: the work is in the hands of the teacher.
PROCESS = frozenset({"evaluation", "grading", "review", "reviewing", "checking", "marking"})
GRADED = frozenset({"graded", "evaluated", "scored", "marked", "checked", "failed", "passed"})
GRADED_STEMS = ("оценен", "проверен", "зачт", "провален")
MISSED = frozenset({"missed", "overdue"})
MISSED_STEMS = ("просроч",)
OPEN = frozenset({"open", "new", "todo", "assigned", "backlog", "progress", "started"})
OPEN_STEMS = ("назначен", "активн")
SUBMITTED = frozenset({"submitted", "done", "completed", "sent"})
SUBMITTED_STEMS = ("сдан", "выполнен", "проверк")  # `на проверке`: handed in, being checked


def words_of(value: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(value)]


def _has(words: list[str], exact: frozenset[str], stems: tuple[str, ...] = ()) -> bool:
    return any(
        word in exact or (_CYRILLIC.fullmatch(word) and word.startswith(stems)) for word in words
    )


def task_state(value: object) -> State:
    if not isinstance(value, str):
        return "unknown"
    words = words_of(value)
    graded = _has(words, GRADED, GRADED_STEMS)
    process = _has(words, PROCESS)
    submitted = _has(words, SUBMITTED, SUBMITTED_STEMS)
    if _has(words, WAITING) and (graded or process):
        return "submitted"  # waiting for the grade: the work is in
    if _has(words, NEGATIONS):
        # "not submitted" is still to do; "not evaluated" says nothing about whether it was handed in
        return "open" if submitted else "unknown"
    if graded:
        return "graded"
    if _has(words, MISSED, MISSED_STEMS):
        return "missed"
    if _has(words, OPEN, OPEN_STEMS) or _has(words, WAITING):
        return "open"
    if submitted or process:
        return "submitted"
    return "unknown"
