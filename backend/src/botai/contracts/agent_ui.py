"""AgentUiBlock — UI blocks an agent sends to the client (contracts/agent-ui.schema.json, ADR-0004)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

Ulid = Annotated[str, Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")]


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)
    id: Ulid
    at: datetime


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)
    title: str = Field(max_length=200)
    url: AnyUrl
    fetched_at: datetime = Field()
    stale: bool = False


QuickReply = Annotated[str, Field(min_length=1, max_length=60)]


class TextBlock(_Block):
    type: Literal["text"] = "text"
    markdown: str = Field(max_length=8000)
    sources: list[Source] | None = Field(default=None, max_length=10)
    quick_replies: list[QuickReply] | None = Field(default=None, max_length=4)


class ThinkingBlock(_Block):
    type: Literal["thinking"] = "thinking"
    stage: str = Field(max_length=120)
    detail: str | None = Field(default=None, max_length=2000)
    state: Literal["running", "ok", "failed", "skipped"] = "running"


class ChoiceOption(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)
    value: str = Field(max_length=120)
    label: str = Field(max_length=200)
    hint: str | None = Field(default=None, max_length=300)


class ChoiceBlock(_Block):
    type: Literal["choice"] = "choice"
    question: str = Field(max_length=500)
    mode: Literal["single", "multi"]
    required: bool
    options: list[ChoiceOption] = Field(min_length=2, max_length=20)
    allow_other: bool = Field(default=False)


class InputBlock(_Block):
    type: Literal["input"] = "input"
    question: str = Field(max_length=500)
    input_kind: Literal["text", "url", "number", "date", "email", "password", "code"]
    placeholder: str | None = Field(default=None, max_length=120)
    required: bool
    max_length: int = Field(default=500, ge=1, le=2000)
    secret: bool = False
    autocomplete: Literal[
        "off", "username", "email", "current-password", "one-time-code", "url"
    ] = "off"


class ConfirmBlock(_Block):
    type: Literal["confirm"] = "confirm"
    question: str = Field(max_length=500)
    confirm_label: str = Field(max_length=40)
    cancel_label: str = Field(max_length=40)


class SummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)
    label: str = Field(max_length=120)
    value: str = Field(max_length=500)
    source_url: AnyUrl | None = Field(default=None)
    confidence: Literal["confirmed", "guess"] | None = None


class SummaryGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)
    title: str = Field(max_length=120)
    items: list[SummaryItem] = Field(max_length=50)


class SummaryBlock(_Block):
    type: Literal["summary"] = "summary"
    title: str = Field(max_length=200)
    groups: list[SummaryGroup] = Field(min_length=1, max_length=12)


class ProposalField(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)
    name: str = Field(max_length=120)
    label: str = Field(max_length=200)
    value: str = Field(max_length=4000)
    editable: bool


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)
    method: Literal["POST", "PUT", "PATCH"]
    url: AnyUrl
    headers: dict[str, str] | None = None
    body_kind: Literal["form", "json", "multipart"] = Field()
    fields: list[ProposalField] = Field(max_length=60)


class ActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)
    id: Ulid
    connection_id: Ulid = Field()
    intent: str = Field(max_length=300)
    reversible: bool
    expires_at: datetime = Field()
    request: ProposalRequest


class ApprovalBlock(_Block):
    type: Literal["approval"] = "approval"
    proposal: ActionProposal


class LoginBlock(_Block):
    type: Literal["login"] = "login"
    connection_id: Ulid = Field()
    ticket: str = Field(max_length=200)
    ws_url: AnyUrl = Field()
    expires_at: datetime = Field()
    hint: str | None = Field(default=None, max_length=500)


class WaitBlock(_Block):
    type: Literal["wait"] = "wait"
    message: str = Field(max_length=300)
    expires_at: datetime | None = Field(default=None)


class HandoffBlock(_Block):
    type: Literal["handoff"] = "handoff"
    title: str = Field(max_length=200)
    url: AnyUrl
    text: str = Field(max_length=8000)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)
    id: str = Field(min_length=1, max_length=40)
    title: str = Field(max_length=200)
    state: Literal["pending", "active", "done", "skipped"]


class PlanBlock(_Block):
    """Answer plan; a block with the same `id` replaces the earlier one in the history."""

    type: Literal["plan"] = "plan"
    title: str = Field(max_length=200)
    steps: list[PlanStep] = Field(min_length=1, max_length=12)


TaskKind = Literal["connect", "repair", "sync", "ingest"]
TaskState = Literal["queued", "running", "waiting_user", "done", "failed", "cancelled"]


class TaskBlock(_Block):
    """Card of a background task; `thread_id` is the thread the task lives in."""

    type: Literal["task"] = "task"
    job_id: Ulid = Field()
    kind: TaskKind
    title: str = Field(max_length=200)
    state: TaskState
    thread_id: Ulid = Field()
    connection_id: Ulid | None = Field(default=None)


class AttachmentBlock(_Block):
    type: Literal["attachment"] = "attachment"
    attachment_id: Ulid = Field()
    name: str = Field(max_length=255)
    mime: str = Field(max_length=120)
    size: int = Field(ge=0)
    kind: Literal["image", "document", "audio", "other"]


AgentUiBlock = Annotated[
    TextBlock
    | ThinkingBlock
    | ChoiceBlock
    | InputBlock
    | ConfirmBlock
    | SummaryBlock
    | ApprovalBlock
    | LoginBlock
    | WaitBlock
    | HandoffBlock
    | PlanBlock
    | TaskBlock
    | AttachmentBlock,
    Field(discriminator="type"),
]

ASKING_TYPES = frozenset({"choice", "input", "confirm", "approval", "login"})


class AgentUiAnswer(BaseModel):
    """Answer to a block. For `input.secret` blocks the value never lands here after the API:
    it is sealed into the Vault (kind=transient) and taken by the worker via JobContext.take_secret."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, alias_generator=to_camel)
    block_id: Ulid = Field()
    values: list[str] | str | bool | None = None
