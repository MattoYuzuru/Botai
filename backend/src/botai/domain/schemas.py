from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from botai.core.api import ApiModel


class Provenance(ApiModel):
    connection_id: str
    space_id: str
    source_url: str  # where the record was read; for an API source this is not a page to open
    fetched_at: datetime
    stale: bool
    # Always present in a response; a default keeps older constructors of the model working.
    open_url: str | None = None  # the page of the source that shows the record (FR-042)


class EventLink(ApiModel):
    url: str
    label: str


class EventAttribute(ApiModel):
    label: str
    value: str


class EventSource(ApiModel):
    """A source that contributed to a class (FR-054): the first one is the university's primary
    schedule source, whose values the class shows."""

    connection_id: str
    title: str  # the human title of the source, never an address


class EventDisagreement(ApiModel):
    """Two sources of one university say different things about a class. The primary source
    decides what the class shows; this says what the other one has, or that the primary has no such
    class at all."""

    kind: Literal[
        "time", "title", "location", "teacher", "group", "address", "several", "not_in_primary"
    ]  # `several`: more than one field differs; `not_in_primary`: see `domain.merge`
    detail: str  # short and human, e.g. «Календарь» — ауд. 305


class ScheduleEventOut(ApiModel):
    id: str
    title: str
    kind: Literal["lecture", "seminar", "lab", "exam", "consultation", "other"]
    starts_at: datetime
    ends_at: datetime
    format: Literal["offline", "online", "hybrid", "unknown"]
    location: str | None
    teacher: str | None
    course_title: str | None
    url: str | None
    changed: bool
    conflicts_with: list[str]  # ids of overlapping events from other spaces (FR-013)
    provenance: Provenance
    kind_text: str | None = None  # the source's own wording of the class type
    address: str | None = None
    group: str | None = None
    lesson_number: int | None = None
    links: list[EventLink] = Field(default_factory=list)
    attributes: list[EventAttribute] = Field(default_factory=list)
    # Every source the class was read from, the primary first; more than one means the class was
    # merged from several sources of one university (FR-054). `provenance` is the primary's.
    sources: list[EventSource] = Field(default_factory=list)
    disagreement: EventDisagreement | None = None


class DeadlineOut(ApiModel):
    id: str
    title: str
    due_at: datetime | None
    effective_due_at: datetime | None
    state: Literal["open", "submitted", "graded", "missed", "unknown"]
    course_title: str | None
    description: str | None = None  # task text from the linked assignment, if the source has it
    url: str | None
    provenance: Provenance


class CourseOut(ApiModel):
    id: str
    title: str
    code: str | None
    term: str | None
    url: str | None
    provenance: Provenance


class GradeOut(ApiModel):
    id: str
    title: str | None
    value: str
    scale: str | None
    weight: float | None
    course_title: str | None
    graded_at: datetime | None
    provenance: Provenance


class PersonOut(ApiModel):
    id: str
    name: str
    role: str
    email: str | None
    provenance: Provenance


class NewsOut(ApiModel):
    id: str
    title: str
    body: str | None
    published_at: datetime
    url: str | None
    provenance: Provenance


class TodayOut(ApiModel):
    """Home screen: today's classes across spaces + nearest deadlines + connection health."""

    date: str
    events: list[ScheduleEventOut]
    deadlines: list[DeadlineOut]
    attention: list[str]  # connection ids that are not ok (needs_reauth, repairing, ...)
