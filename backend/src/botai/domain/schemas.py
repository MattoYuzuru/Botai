from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from botai.core.api import ApiModel


class Provenance(ApiModel):
    """Where a record was read and when. Two provenance paths coexist while v3 and v4 share the
    domain tables: a v3 row names its `connectionId`, a v4 row its `sourceId` (docs/v4/03 §6.1).
    Both are null-capable for that reason; a response always has at least one of them."""

    connection_id: str | None = None  # provenance of the v3 reading path
    source_id: str | None = None  # provenance of the v4 reading path
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
    schedule source, whose values the class shows. A v3 source names its connection, a v4 source
    its source record; exactly one of the two is set."""

    connection_id: str | None = None
    source_id: str | None = None
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


TaskState = Literal["open", "submitted", "graded", "missed", "unknown"]
GradeLayer = Literal["item", "current", "final", "summary"]


class DeadlineGradeOut(ApiModel):
    """The grade of a task: the latest grade of the same source for this task (D-043)."""

    value: str
    scale: str | None
    value_num: float | None
    scale_max: float | None
    url: str | None


class DeadlineOut(ApiModel):
    id: str
    title: str
    due_at: datetime | None
    effective_due_at: datetime | None
    # The effective state: the student's mark, else the source's state with the status rules
    # applied (D-045); `state_source` says which decided it.
    state: TaskState
    course_title: str | None
    description: str | None = None  # task text from the linked assignment, if the source has it
    url: str | None
    provenance: Provenance
    state_text: str | None = None  # the status in the source's own words («Решение прикреплено»)
    state_source: Literal["student", "rule", "source"] = "source"
    changed_at: datetime | None = None  # the last change the student should notice (§5.5)
    grade: DeadlineGradeOut | None = None


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
    # Set only when the source printed both a plain number and the top of its scale (D-039);
    # never computed or converted. `value` and `scale` stay the source's own text.
    value_num: float | None = None
    scale_max: float | None = None
    course_id: str | None = None  # `CourseOut.id` of the grade's course, when the source links one
    # D-043: the layer (null — the source did not say; shown as an item), the term or module, the
    # graded task, the page of the grade, the grader's comment, the last noticeable change.
    layer: GradeLayer | None = None
    period: str | None = None
    assignment_id: str | None = None
    url: str | None = None
    comment: str | None = None
    changed_at: datetime | None = None


class ProfileFieldOut(ApiModel):
    """One field of the student's card exactly as the source printed it."""

    id: str
    field: str
    label: str
    value: str
    group: str | None
    provenance: Provenance


class RankingEntryOut(ApiModel):
    """One place on a rating board; `score` is the source's text, `score_num` its plain number."""

    id: str
    board: str
    position: int
    label: str
    score: str | None
    score_num: float | None
    is_me: bool
    total: int | None
    period: str | None
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


class UpdateOut(ApiModel):
    """One recent change of the student's data: a grade that appeared or changed, a task whose
    state changed (docs/v4/27 §5.5). Grade fields are set for `grade`, state fields for
    `deadline`."""

    kind: Literal["grade", "deadline"]
    id: str
    title: str | None
    course_title: str | None
    changed_at: datetime
    url: str | None
    provenance: Provenance
    value: str | None = None
    scale: str | None = None
    value_num: float | None = None
    scale_max: float | None = None
    layer: GradeLayer | None = None
    state: TaskState | None = None
    state_text: str | None = None


class StatusRuleOut(ApiModel):
    """A rule of the student: the status `label` of a source (or of every source) means `state`."""

    id: str
    source_id: str | None
    source_title: str | None
    label: str
    state: Literal["open", "submitted", "graded", "missed"]
    created_at: datetime


class StatusRuleIn(ApiModel):
    source_id: str | None = None
    label: str = Field(min_length=1, max_length=200)
    state: Literal["open", "submitted", "graded", "missed"]


class DeadlinePatchIn(ApiModel):
    """The student's mark on one task; null clears it."""

    user_state: Literal["open", "submitted", "graded"] | None


class TodayOut(ApiModel):
    """Home screen: today's classes across spaces + nearest deadlines + connection health."""

    date: str
    events: list[ScheduleEventOut]
    deadlines: list[DeadlineOut]
    attention: list[str]  # connection ids that are not ok (needs_reauth, repairing, ...)
    updates: list[UpdateOut] = Field(default_factory=list)  # «Новое»: the last 3 days, at most 5
