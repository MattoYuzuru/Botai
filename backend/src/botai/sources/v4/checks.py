"""Warnings of a run: typical parser mistakes the agent should fix (D-043 §3.5).

Pure functions over the normalized records of one probe/canary/sync. A warning never blocks a
write and never fails a run: it is a hint addressed to the agent, in Russian, that says what is
wrong and how to fix the playbook. Codes (docs/v4/27-study-hub-grades-fastpath.md §3.5):

* `generic_links` — at least 3 records of a kind of entities and all of them lead to one page;
* `unknown_states` — at least 30 % of the deadlines have the state `unknown`;
* `grades_without_numbers` — grades with a numeric value but no top of the scale;
* `no_item_grades` — tasks with a maximum score are graded, but no grade of a work is written;
* `courses_missing` — records name a `courseId` that no `courses` record of the run (nor a course
  already stored for the source) has: the subject has no title and nothing can be grouped by it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from botai.sources.grade_scale import plain_number

LINK_KINDS: tuple[str, ...] = ("assignments", "deadlines", "grades", "schedule")
MIN_GENERIC_LINKS = 3
UNKNOWN_SHARE = 0.3
MAX_EXAMPLES = 8


class RunWarning(BaseModel):
    """A hint to the agent about the records of a run; not an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    kinds: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class CheckedRecord(Protocol):
    """What the checks read of a normalized record (`runner.NormalizedRecord`)."""

    @property
    def kind(self) -> str: ...

    @property
    def external_id(self) -> str: ...

    @property
    def fields(self) -> Mapping[str, Any]: ...

    @property
    def open_url(self) -> str | None: ...


def _of(records: Sequence[CheckedRecord], kind: str) -> list[CheckedRecord]:
    return [record for record in records if record.kind == kind]


# Kinds whose records always have a page in a learning system: a record without its own `url` falls
# back to the read's `open_url`, and a task list on every task is exactly the mistake to flag. A
# class or a grade often has no page of its own: only an explicit `url` field is compared there.
OPEN_URL_KINDS = frozenset({"assignments", "deadlines"})


def _own_url(record: CheckedRecord) -> str | None:
    url = record.fields.get("url")
    return url if isinstance(url, str) and url else None


def _link(record: CheckedRecord) -> str | None:
    own = _own_url(record)
    if own is not None or record.kind not in OPEN_URL_KINDS:
        return own
    return record.open_url or None


def _per_record(link: str, records: Sequence[CheckedRecord]) -> bool:
    """A link that names one of the records (its id is a whole token of the address) is that
    record's page, not a list."""
    return any(
        record.external_id
        and re.search(rf"(?<![0-9A-Za-z]){re.escape(record.external_id)}(?![0-9A-Za-z])", link)
        for record in records
    )


def generic_links(records: Sequence[CheckedRecord]) -> RunWarning | None:
    kinds: list[str] = []
    examples: list[str] = []
    for kind in LINK_KINDS:
        linked = [(record, _link(record)) for record in _of(records, kind)]
        linked = [(record, link) for record, link in linked if link is not None]
        if len(linked) < MIN_GENERIC_LINKS:
            continue
        links = {link for _, link in linked}
        if len(links) != 1:
            continue
        (link,) = links
        if _per_record(link, [record for record, _ in linked]):
            continue
        kinds.append(kind)
        examples.append(link)
    if not kinds:
        return None
    return RunWarning(
        code="generic_links",
        message=(
            "У всех записей этих видов одна и та же ссылка — она ведёт на список или главную, а не "
            "на саму сущность. Собери url каждой записи из идентификаторов ответа (курс, тема, "
            "задание, пара, оценка) и проверь одну ссылку в браузере. Если у сущности нет своей "
            "страницы — оставь url пустым: не придумывай адрес."
        ),
        kinds=kinds,
        examples=examples[:MAX_EXAMPLES],
    )


def unknown_states(records: Sequence[CheckedRecord]) -> RunWarning | None:
    deadlines = _of(records, "deadlines")
    unknown = [record for record in deadlines if record.fields.get("state") == "unknown"]
    if not deadlines or len(unknown) < UNKNOWN_SHARE * len(deadlines):
        return None
    texts = Counter(str(record.fields.get("state_text") or "—") for record in unknown).most_common(
        MAX_EXAMPLES
    )
    return RunWarning(
        code="unknown_states",
        message=(
            f"У {len(unknown)} из {len(deadlines)} дедлайнов состояние unknown: словарь не понял "
            "статус. Пиши state одним из open, submitted, graded, missed, если знаешь его "
            "наверняка, а подпись с сайта — в stateText. Если подпись означает другое состояние, "
            "чем решил словарь, предложи студенту правило статуса."
        ),
        kinds=["deadlines"],
        examples=[text for text, _ in texts],
    )


def grades_without_numbers(records: Sequence[CheckedRecord]) -> RunWarning | None:
    missing = [
        record
        for record in _of(records, "grades")
        if record.fields.get("scale_max") is None
        and plain_number(record.fields.get("value")) is not None
    ]
    if not missing:
        return None
    examples = [
        f"{record.fields.get('title') or record.external_id}: {record.fields.get('value')}"
        for record in missing[:MAX_EXAMPLES]
    ]
    return RunWarning(
        code="grades_without_numbers",
        message=(
            f"У {len(missing)} числовых оценок нет максимума шкалы: они не попадут на графики и "
            "в долю от максимума. Если источник показывает максимум (maxScore, «из 10», "
            "10-балльная), пиши его в max числом или в scale текстом. Не придумывай шкалу, "
            "которой источник не дал."
        ),
        kinds=["grades"],
        examples=examples,
    )


def no_item_grades(records: Sequence[CheckedRecord]) -> RunWarning | None:
    scored = [
        record
        for record in _of(records, "assignments")
        if record.fields.get("max_score") is not None
    ]
    graded = [
        record for record in _of(records, "deadlines") if record.fields.get("state") == "graded"
    ]
    if not scored or not graded:
        return None
    items = [
        record
        for record in _of(records, "grades")
        if record.fields.get("layer") == "item" or record.fields.get("assignment_external_id")
    ]
    if items:
        return None
    return RunWarning(
        code="no_item_grades",
        message=(
            "Есть проверенные задания с максимальным баллом, но ни одной оценки за работу. "
            "Прочитай оценённые задания (часто это отдельный фильтр или архив: evaluated, graded, "
            "«проверено») и пиши балл каждой работы записью grades с value, max, assignmentId, "
            "url и layer: item."
        ),
        kinds=["grades", "assignments"],
        examples=[str(record.fields.get("title") or record.external_id) for record in graded][
            :MAX_EXAMPLES
        ],
    )


COURSE_REFERRING_KINDS: tuple[str, ...] = ("grades", "deadlines", "assignments", "schedule")


def referenced_courses(records: Sequence[CheckedRecord]) -> set[str]:
    """The `courseId` values the records of other kinds name, minus those of this run's courses."""
    named = {
        str(course_id)
        for kind in COURSE_REFERRING_KINDS
        for record in _of(records, kind)
        if (course_id := record.fields.get("course_external_id"))
    }
    return named - {record.external_id for record in _of(records, "courses")}


def courses_missing(
    records: Sequence[CheckedRecord], stored: frozenset[str] = frozenset()
) -> RunWarning | None:
    missing = sorted(referenced_courses(records) - stored)
    if not missing:
        return None
    return RunWarning(
        code="courses_missing",
        message=(
            f"Записи ссылаются на {len(missing)} courseId, для которых нет записи courses: у "
            "предметов не будет названия, и оценки и задания нельзя сгруппировать. Для каждого "
            "упомянутого курса пиши запись courses с external_id, равным этому courseId, title "
            "(название предмета) и url страницы курса."
        ),
        kinds=["courses"],
        examples=missing[:MAX_EXAMPLES],
    )


CHECKS = (generic_links, unknown_states, grades_without_numbers, no_item_grades)


def run_warnings(
    records: Sequence[CheckedRecord], *, stored_courses: frozenset[str] = frozenset()
) -> list[RunWarning]:
    """Every warning the records of one run deserve, in the order of the codes. `stored_courses`
    are the course ids the source already has in the domain (they need no new record)."""
    found = [warning for check in CHECKS if (warning := check(records)) is not None]
    if (warning := courses_missing(records, stored_courses)) is not None:
        found.append(warning)
    return found
