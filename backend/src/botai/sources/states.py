"""The state of a task in the words of a source, as one of the states the domain knows.

A source names a state however it likes: `inProgress`, `IN_PROGRESS`, `in-progress`, `Не сдано`.
The value is split into words (case, `_`, `-`, spaces) and each word is matched whole against a
small generic vocabulary. A substring is never enough: `preview` is not `review`, `reviewed` is not
`review`, and a word that negates or postpones (`not`, `pending`, `awaiting`, `не`) is read before
the word it stands next to: `notEvaluated` is not `evaluated`.

The vocabulary is the common one of learning systems, not of one cabinet. What it does not know is
`unknown`, which keeps a task on the student's list rather than hiding it. For the student,
`submitted` and `graded` mean "nothing is needed from me"; a work sent back for rework, returned
or still a draft is `open` (docs/v4/27-study-hub-grades-fastpath.md §3.3).
"""

from __future__ import annotations

import re
from typing import Literal

State = Literal["open", "submitted", "graded", "missed", "unknown"]
STATES: tuple[State, ...] = ("open", "submitted", "graded", "missed", "unknown")

# English words are matched whole; Russian ones by stem, since the ending follows the noun.
_WORD = re.compile(r"[A-ZА-ЯЁ]?[a-zа-яё]+|[A-ZА-ЯЁ]+(?![a-zа-яё])")
_CYRILLIC = re.compile(r"[а-яё]+")

NEGATIONS = frozenset({"not", "no", "never", "without", "не", "нет", "без"})
WAITING = frozenset({"pending", "awaiting", "await", "waiting", "wait", "ожидает", "ожидание"})
# Being checked, or having been: the work is in the hands of the teacher.
PROCESS = frozenset({"evaluation", "grading", "review", "reviewing", "checking", "marking"})
PROCESS_STEMS = ("проверк", "оценивани")  # `на проверке`, `ожидает проверки`
GRADED = frozenset({"graded", "evaluated", "scored", "marked", "checked", "failed", "passed"})
GRADED_STEMS = ("оценен", "проверен", "зачт", "провален")
# «выставлен…» is a grade only next to the word of a grade: «Оценка выставлена», «Балл выставлен»
# (not «Задание выставлено», «Выставлен срок сдачи»).
SET_STEM = "выставлен"
GRADE_NOUN_STEMS = ("оценк", "балл", "отметк")
# «принят…» is a grade only as the status of a work, a short label of these words: «Принято»,
# «Работа принята», «Решение принято» (not «Принят к сведению», «Заявка принята»).
ACCEPTED_STEM = "принят"
WORK_NOUNS = frozenset({"работа", "решение", "задание", "ответ"})
MAX_ACCEPTED_WORDS = 3
MISSED = frozenset({"missed", "overdue"})
MISSED_STEMS = ("просроч",)
# A draft is not handed in yet. `returned` alone is not here: Classroom returns graded work.
OPEN = frozenset(
    {"open", "new", "todo", "assigned", "backlog", "progress", "started", "draft", "reopened",
     "rework"}
)  # fmt: skip
OPEN_STEMS = ("назначен", "активн", "черновик")
# Sent back for rework: it wins over a grade or a hand-in in the same status («Возвращено на
# доработку», «Проверено, требует доработки»). The noun only: «доработано» says the student did it.
REWORK = frozenset({"rework", "revision", "resubmit"})
REWORK_NOUN_STEM = "доработк"
# «после доработки» is history, not the current state («Отправлено после доработки»).
AFTER = frozenset({"после", "after"})
# `задан…` as a stem would also take `задание` (the noun of almost every title): whole words only.
OPEN_WORDS = frozenset({"задан", "задано", "задана", "заданы"})
# Two words that are open together and nothing alone (`работа` is the noun of `Работа принята`).
OPEN_PHRASES = (("в", "работе"),)
SUBMITTED = frozenset({"submitted", "done", "completed", "sent", "uploaded", "attached"})
# `доработан…`: the student did the rework and handed the work in again.
SUBMITTED_STEMS = (
    "сдан", "выполнен", "отправлен", "прикреплен", "прикреплён", "загружен", "доработан",
)  # fmt: skip
# A negated word of handing in, accepting or starting means the work is still to do:
# `не сдано`, `не отправлено`, `не принято`, `не начато`, `notStarted`. A negated grade says
# nothing about whether the work was handed in (`не оценено` stays unknown).
NEGATED_OPEN = frozenset({"started", "accepted", "approved"})
NEGATED_OPEN_STEMS = ("начат",)


# Every word a status of a task is made of, in any learning system: the words above and the glue
# and the few others that statuses share. A status filter is words from here and nothing else.
STATUS_WORDS = (
    NEGATIONS
    | WAITING
    | PROCESS
    | GRADED
    | MISSED
    | OPEN
    | SUBMITTED
    | frozenset(
        {
            "in", "on", "to", "for", "of", "by", "with", "has", "have", "is", "was", "need",
            "needs", "solution", "revision", "rejected", "accepted", "approved", "declined",
            "returned", "reopened", "resubmitted", "draft", "closed", "active", "archived",
            "published", "hidden", "finished", "ready", "late", "expired", "cancelled",
            "canceled", "skipped", "excused", "waiting",
        }
    )
)  # fmt: skip
_PLAIN = re.compile(r"[A-Za-z]+(?:[ _-]?[A-Za-z]+)*")


def words_of(value: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(value)]


def _has(words: list[str], exact: frozenset[str], stems: tuple[str, ...] = ()) -> bool:
    return any(
        word in exact or (_CYRILLIC.fullmatch(word) and word.startswith(stems)) for word in words
    )


def _phrase(words: list[str], phrases: tuple[tuple[str, ...], ...]) -> bool:
    return any(
        tuple(words[i : i + len(phrase)]) == phrase
        for phrase in phrases
        for i in range(len(words) - len(phrase) + 1)
    )


def _open(words: list[str]) -> bool:
    return (
        _has(words, OPEN, OPEN_STEMS)
        or any(word in OPEN_WORDS for word in words)
        or _phrase(words, OPEN_PHRASES)
    )


def _accepted(words: list[str]) -> bool:
    """«Принято», «Работа принята», «Решение не принято»: the acceptance of a work, nothing else."""
    rest = [word for word in words if word not in NEGATIONS]
    return (
        1 <= len(rest) <= MAX_ACCEPTED_WORDS
        and any(word.startswith(ACCEPTED_STEM) for word in rest)
        and all(word.startswith(ACCEPTED_STEM) or word in WORK_NOUNS for word in rest)
    )


def _grade_set(words: list[str]) -> bool:
    """«Оценка выставлена», «Баллы выставлены»: a grade word next to «выставлен…»."""
    return any(word.startswith(SET_STEM) for word in words) and _has(
        words, frozenset(), GRADE_NOUN_STEMS
    )


def _rework(words: list[str]) -> bool:
    """Sent back for rework now: «на доработке», «требует доработки», «needs revision» — but not
    «после доработки», which tells the history of a work handed in again."""
    for index, word in enumerate(words):
        if index and words[index - 1] in AFTER:
            continue
        if word in REWORK or word.startswith(REWORK_NOUN_STEM):
            return True
    return False


def task_state(value: object) -> State:
    if not isinstance(value, str):
        return "unknown"
    words = words_of(value)
    accepted = _accepted(words)
    graded = _has(words, GRADED, GRADED_STEMS) or _grade_set(words) or accepted
    process = _has(words, PROCESS, PROCESS_STEMS)
    submitted = _has(words, SUBMITTED, SUBMITTED_STEMS)
    if _has(words, WAITING) and (graded or process):
        return "submitted"  # waiting for the grade: the work is in
    if _has(words, NEGATIONS):
        # "not submitted" is still to do; "not evaluated" says nothing about whether it was handed in
        negated_open = submitted or accepted or _has(words, NEGATED_OPEN, NEGATED_OPEN_STEMS)
        return "open" if negated_open else "unknown"
    if _rework(words):
        return "open"  # sent back for rework outweighs a grade or a hand-in next to it
    if graded:
        return "graded"
    if _has(words, MISSED, MISSED_STEMS):
        return "missed"
    if _open(words) or _has(words, WAITING):
        return "open"
    if submitted or process:
        return "submitted"
    return "unknown"


def is_status(value: str) -> bool:
    """A value that is only words of the status vocabulary (`inProgress`, `IN_PROGRESS`,
    `awaiting-review`). A name, a code or a login is not, whatever its letters look like."""
    words = words_of(value)
    return (
        bool(words)
        and _PLAIN.fullmatch(value) is not None
        and all(w in STATUS_WORDS for w in words)
    )
