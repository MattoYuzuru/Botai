from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from botai.core.api import ApiModel
from botai.jobs.schemas import JobOut

ULID = r"^[0-9A-HJKMNP-TV-Z]{26}$"
Attention = Literal["waiting_user", "running", "failed"]


class ThreadCreate(ApiModel):
    space_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    client_request_id: str | None = Field(default=None, pattern=ULID)


class ThreadPatch(ApiModel):
    title: str = Field(min_length=1, max_length=200)


class ReadIn(ApiModel):
    """The last message the client showed; without it everything written so far is read."""

    up_to: datetime | None = None


class ActiveTask(ApiModel):
    job_id: str
    kind: Literal["connect", "repair", "sync", "ingest"]
    connection_id: str | None
    state: str
    title: str


class ThreadOut(ApiModel):
    id: str
    space_id: str | None
    title: str
    created_at: datetime
    last_message_at: datetime
    unread: bool  # an agent message is newer than last_read_at
    attention: Attention | None  # the active task or turn: waiting for the student, running, failed
    active_task: ActiveTask | None
    preview: str | None  # start of the last text message, up to 120 characters


class MessageIn(ApiModel):
    """The text may be empty only when files are attached."""

    text: str = Field(default="", max_length=8000)
    attachment_ids: list[Annotated[str, Field(pattern=ULID)]] = Field(
        default_factory=list, max_length=10
    )
    client_message_id: str | None = Field(default=None, pattern=ULID)
    target_job_id: str | None = Field(default=None, pattern=ULID)

    @model_validator(mode="after")
    def something_is_sent(self) -> MessageIn:
        if not self.text.strip() and not self.attachment_ids:
            raise ValueError("a message needs text or files")
        return self


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
    """The live state of a thread. `tasks` are its newest tasks; `active_job` is the one that
    needs attention first (a task, else the running agent turn)."""

    active_job: JobOut | None
    tasks: list[JobOut]
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


class MemoryNoteOut(ApiModel):
    id: str
    note: str
    created_at: datetime


class ApproveIn(ApiModel):
    fields: dict[str, str]  # final values after the user's edits
    fields_hash: str  # sha256 of canonical fields the user saw
