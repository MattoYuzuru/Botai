"""Panel spec v1 and the API models of panels (D-039).

The spec is data, never code: strings, numbers and enums from a closed vocabulary. Free text is
plain (no HTML, markdown or links) and short; the icon is a name of the design registry
(`packages/design/src/ui/icon/registry.ts`, checked by a test of the web package); lists have hard
limits that match the chart limits of the design system (`CHART_LIMITS`: 50 labels, 6 series).
The union is discriminated by `type`, which is also the `panels.type` column.

The same models serve the agent's proposals (`panels.service.propose_panel` validates a raw
spec with `parse_spec`) and the API responses (`PanelOut.spec`), so what the agent can write and
what the client can render is one vocabulary. A client meets only valid specs: whatever fails
validation is not stored.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from botai.core.api import ApiModel
from botai.core.errors import BotaiError, ErrorCode

SPEC_VERSION = 1

# The names the design registry has; the web package test checks every one of them exists there.
PanelIcon = Literal[
    "grade",
    "course",
    "person",
    "user",
    "university",
    "schedule",
    "deadline",
    "table",
    "number",
    "group",
    "teacher",
    "today",
    "news",
    "document",
    "chart",
    "trophy",
    "star",
    "idCard",
    "trend",
]
PANEL_ICONS: tuple[str, ...] = get_args(PanelIcon)

PanelType = Literal["kpi", "chart", "table", "ranking", "profile", "list"]
PanelStatus = Literal["proposed", "active", "dismissed"]
PanelOrigin = Literal["system", "agent"]
DatasetKind = Literal["grades", "profile", "ranking"]

# Limits of the views; the chart ones repeat `CHART_LIMITS` of the design system.
MAX_TITLE = 60
MAX_SELECTOR = 120
MAX_CHART_LABELS = 50
MAX_CHART_SERIES = 6
MAX_TABLE_ROWS = 100
MAX_LIST_ROWS = 50
MAX_RANKING_TOP = 50
MAX_PROFILE_FIELDS = 30

_ULID = r"^[0-9A-HJKMNP-TV-Z]{26}$"
_NOT_PLAIN = re.compile(
    r"[<>`]|://|\bwww\.|\]\(|\*\*|__|(?:javascript|vbscript):|(?:^|\n)\s*(?:#|[-*+]\s|\d+[.)]\s)",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _plain(value: str) -> str:
    """One line of plain text: no HTML, markdown, links or control characters."""
    spaced = value.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = " ".join(spaced.split())
    if not text:
        raise ValueError("must not be empty")
    if _NOT_PLAIN.search(text) or _CONTROL.search(spaced):
        raise ValueError("plain text only: no HTML, markdown, links or control characters")
    return text


def _selector(value: str) -> str:
    """A value that picks rows (a board, a field key): kept as is, only control characters out."""
    if _CONTROL.search(value) or not value.strip():
        raise ValueError("must be a non-empty single line")
    return value


PlainTitle = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_TITLE), AfterValidator(_plain)
]
Selector = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_SELECTOR), AfterValidator(_selector)
]
UlidId = Annotated[str, StringConstraints(pattern=_ULID)]


class _Strict(ApiModel):
    """camelCase like the rest of the API; unknown fields are an error, not ignored, and a value
    of the wrong type is not coerced (`"yes"` is not a flag, `"5"` is not a limit)."""

    model_config = ConfigDict(extra="forbid", strict=True)


# --- spec ---------------------------------------------------------------------------------------


class Dataset(_Strict):
    """Which data a panel shows, by kind: the rows themselves are never in the spec."""

    kind: DatasetKind
    board: Selector | None = None  # ranking only: the board as the source names it
    course_id: UlidId | None = None  # grades only: `CourseOut.id`


KpiMetric = Literal["count", "latest", "best"]
TableColumn = Literal["title", "course", "value", "scale", "date"]


class KpiView(_Strict):
    """Tiles: how many grades, the latest one, the best one of the most common scale."""

    metrics: list[KpiMetric] = Field(default_factory=lambda: ["count", "latest"], max_length=3)

    @model_validator(mode="after")
    def _distinct(self) -> KpiView:
        if not self.metrics or len(set(self.metrics)) != len(self.metrics):
            raise ValueError("metrics must be a non-empty list without repeats")
        return self


class ChartView(_Strict):
    """Individual grades over time or by course, in the source's own scale: no averages."""

    variant: Literal["bar", "line"] = "line"
    x: Literal["date", "course"] = "date"
    limit: int = Field(default=24, ge=1, le=MAX_CHART_LABELS)


class TableView(_Strict):
    columns: list[TableColumn] = Field(
        default_factory=lambda: ["title", "course", "value", "date"], max_length=5
    )
    limit: int = Field(default=20, ge=1, le=MAX_TABLE_ROWS)

    @model_validator(mode="after")
    def _distinct(self) -> TableView:
        if not self.columns or len(set(self.columns)) != len(self.columns):
            raise ValueError("columns must be a non-empty list without repeats")
        return self


class RankingView(_Strict):
    top: int = Field(default=10, ge=1, le=MAX_RANKING_TOP)
    highlight_me: bool = True


class ProfileView(_Strict):
    """Field keys in the order to show; empty means every field the source gave."""

    fields: list[Selector] = Field(default_factory=list, max_length=MAX_PROFILE_FIELDS)


class ListView(_Strict):
    limit: int = Field(default=10, ge=1, le=MAX_LIST_ROWS)


class _PanelBase(_Strict):
    v: Literal[1] = SPEC_VERSION
    title: PlainTitle
    icon: PanelIcon
    dataset: Dataset

    def _check_dataset(self, kind: DatasetKind) -> None:
        if self.dataset.kind != kind:
            raise ValueError(f"this panel type shows the {kind} dataset")
        if kind == "ranking" and self.dataset.board is None:
            raise ValueError("a ranking panel needs dataset.board")
        if kind != "ranking" and self.dataset.board is not None:
            raise ValueError("dataset.board is only for the ranking dataset")
        if kind != "grades" and self.dataset.course_id is not None:
            raise ValueError("dataset.courseId is only for the grades dataset")


class KpiSpec(_PanelBase):
    type: Literal["kpi"]
    view: KpiView = Field(default_factory=KpiView)

    @model_validator(mode="after")
    def _dataset(self) -> KpiSpec:
        self._check_dataset("grades")
        return self


class ChartSpec(_PanelBase):
    type: Literal["chart"]
    view: ChartView = Field(default_factory=ChartView)

    @model_validator(mode="after")
    def _dataset(self) -> ChartSpec:
        self._check_dataset("grades")
        return self


class TableSpec(_PanelBase):
    type: Literal["table"]
    view: TableView = Field(default_factory=TableView)

    @model_validator(mode="after")
    def _dataset(self) -> TableSpec:
        self._check_dataset("grades")
        return self


class RankingSpec(_PanelBase):
    type: Literal["ranking"]
    view: RankingView = Field(default_factory=RankingView)

    @model_validator(mode="after")
    def _dataset(self) -> RankingSpec:
        self._check_dataset("ranking")
        return self


class ProfileSpec(_PanelBase):
    type: Literal["profile"]
    view: ProfileView = Field(default_factory=ProfileView)

    @model_validator(mode="after")
    def _dataset(self) -> ProfileSpec:
        self._check_dataset("profile")
        return self


class ListSpec(_PanelBase):
    type: Literal["list"]
    view: ListView = Field(default_factory=ListView)

    @model_validator(mode="after")
    def _dataset(self) -> ListSpec:
        self._check_dataset("grades")
        return self


PanelSpec = Annotated[
    KpiSpec | ChartSpec | TableSpec | RankingSpec | ProfileSpec | ListSpec,
    Field(discriminator="type"),
]
PanelSpecModel = KpiSpec | ChartSpec | TableSpec | RankingSpec | ProfileSpec | ListSpec

_SPEC = TypeAdapter[PanelSpecModel](PanelSpec)


def parse_spec(raw: Any) -> PanelSpecModel:
    """The validated spec, or `BotaiError(VALIDATION)` naming where it is wrong (never the
    offending value: it may be arbitrary text from a model)."""
    try:
        return _SPEC.validate_python(raw)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']) or 'spec'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)[:5]
        ]
        raise BotaiError(
            ErrorCode.VALIDATION, "Спецификация панели не прошла проверку: " + "; ".join(problems)
        ) from None


def dump_spec(spec: PanelSpecModel) -> dict[str, Any]:
    """The stored form of a spec: camelCase JSON without empty selectors."""
    return spec.model_dump(by_alias=True, mode="json", exclude_none=True)


# --- resolved data ------------------------------------------------------------------------------


class GradePointOut(ApiModel):
    """One grade as the source gave it; `value_num`/`scale_max` only when both were plain."""

    id: str
    title: str | None
    course: str | None
    course_id: str | None
    value: str
    scale: str | None
    value_num: float | None
    scale_max: float | None
    graded_at: datetime | None


class GradesData(ApiModel):
    """`total` counts every grade of the dataset; `points` are the newest ones within the view's
    limit (a chart: only numeric ones, at most `MAX_CHART_SERIES` scales); `latest`/`best` are
    taken from the whole dataset."""

    kind: Literal["grades"] = "grades"
    total: int
    numeric: int
    points: list[GradePointOut]
    latest: GradePointOut | None
    best: GradePointOut | None


class ProfileFieldData(ApiModel):
    field: str
    label: str
    value: str
    group: str | None


class ProfileData(ApiModel):
    kind: Literal["profile"] = "profile"
    total: int
    fields: list[ProfileFieldData]


class RankingRowData(ApiModel):
    position: int
    label: str
    score: str | None
    score_num: float | None
    is_me: bool
    total: int | None
    period: str | None


class RankingData(ApiModel):
    """The top of one board plus the student's own row when it is lower; `total` counts the rows
    the board has, `participants` is the size the source reported."""

    kind: Literal["ranking"] = "ranking"
    board: str
    total: int
    participants: int | None
    period: str | None
    rows: list[RankingRowData]


PanelData = Annotated[GradesData | ProfileData | RankingData, Field(discriminator="kind")]


# --- API ----------------------------------------------------------------------------------------


class PanelSpaceOut(ApiModel):
    """The university a panel belongs to: enough to group panels in the all-universities mode."""

    id: str
    title: str
    short_title: str
    accent_hue: float


class PanelOut(ApiModel):
    id: str
    key: str
    type: PanelType
    spec: PanelSpec
    status: PanelStatus
    visible: bool
    hidden_in_all: bool
    position: int
    origin: PanelOrigin
    source_id: str | None
    created_at: datetime
    updated_at: datetime
    space: PanelSpaceOut
    # Resolved, bounded data of the dataset; null for a panel the student has switched off
    # (nothing is read for it) and for a dismissed one.
    data: PanelData | None


class PanelPatch(_Strict):
    """The student's switches; omitted fields stay as they are."""

    visible: bool | None = None
    hidden_in_all: bool | None = None
    position: int | None = Field(default=None, ge=0, le=10_000)
