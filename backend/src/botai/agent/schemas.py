from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from botai.contracts.specs import Kind
from botai.core.api import ApiModel
from botai.jobs.schemas import JobOut
from botai.spaces.schemas import ConnectionKind, ConnectionOut

ULID = r"^[0-9A-HJKMNP-TV-Z]{26}$"


class ConnectContext(ApiModel):
    university_id: str | None = Field(default=None, min_length=1, max_length=50)
    entry_urls: list[Annotated[str, Field(max_length=1000)]] = Field(
        default_factory=list, max_length=6
    )
    kind: ConnectionKind | None = None
    title: str | None = Field(default=None, max_length=120)
    requested_kinds: list[Kind] = Field(default_factory=lambda: ["scheduleEvents"])
    absent_kinds: list[Kind] = Field(default_factory=list)
    user_note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def distinct_kinds(self) -> ConnectContext:
        if len(set(self.requested_kinds)) != len(self.requested_kinds) or len(
            set(self.absent_kinds)
        ) != len(self.absent_kinds):
            raise ValueError("data kinds must be unique")
        if set(self.requested_kinds) & set(self.absent_kinds):
            raise ValueError("requested and absent kinds must be disjoint")
        return self


class ThreadCreate(ApiModel):
    space_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    purpose: Literal["chat", "connect"] = "chat"
    connect_context: ConnectContext | None = None
    client_request_id: str | None = Field(default=None, pattern=ULID)


class ThreadOut(ApiModel):
    id: str
    space_id: str | None
    title: str
    created_at: datetime
    updated_at: datetime


class MessageIn(ApiModel):
    text: str = Field(min_length=1, max_length=8000)
    client_message_id: str | None = Field(default=None, pattern=ULID)
    target_job_id: str | None = Field(default=None, pattern=ULID)


class MessageOut(ApiModel):
    id: str
    role: Literal["user", "agent"]
    blocks: list[dict[str, Any]]  # AgentUiBlock[] (contracts/agent-ui.schema.json)
    job_id: str | None
    created_at: datetime


class MessageAccepted(ApiModel):
    message: MessageOut
    job_id: str  # agent reply streams via /jobs/{id}/events


class PendingRef(ApiModel):
    job_id: str
    block_id: str
    channel: Literal["native", "dialogue", "manual", "approval"]
    secret: bool


class DialogueReceipt(ApiModel):
    job_id: str
    message_id: str | None
    block_id: str | None
    status: Literal["accepted", "applied", "superseded", "rejected"]
    at: datetime

    @model_validator(mode="after")
    def one_reference(self) -> DialogueReceipt:
        if (self.message_id is None) == (self.block_id is None):
            raise ValueError("a receipt identifies exactly one message or block")
        return self


class ThreadRuntime(ApiModel):
    purpose: Literal["chat", "connect"]
    active_job: JobOut | None
    connection: ConnectionOut | None
    pending: list[PendingRef]
    receipts: list[DialogueReceipt]


class ContextSegment(ApiModel):
    kind: Literal["system", "profile", "data", "documents", "history", "free"]
    label: str
    tokens: int


class ContextSource(ApiModel):
    title: str
    kind: str
    fetched_at: datetime | None


class ContextOut(ApiModel):
    """What is in the agent's context for this thread (agent-harness.md §6)."""

    limit: int
    used: int
    segments: list[ContextSegment]
    sources: list[ContextSource]


class ApproveIn(ApiModel):
    fields: dict[str, str]  # final values after the user's edits
    fields_hash: str  # sha256 of canonical fields the user saw
