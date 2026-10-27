from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from botai.core.api import ApiModel


class ThreadCreate(ApiModel):
    space_id: str | None = None
    title: str | None = Field(default=None, max_length=200)


class ThreadOut(ApiModel):
    id: str
    space_id: str | None
    title: str
    created_at: datetime
    updated_at: datetime


class MessageIn(ApiModel):
    text: str = Field(min_length=1, max_length=8000)


class MessageOut(ApiModel):
    id: str
    role: Literal["user", "agent"]
    blocks: list[dict[str, Any]]  # AgentUiBlock[] (contracts/agent-ui.schema.json)
    job_id: str | None
    created_at: datetime


class MessageAccepted(ApiModel):
    message: MessageOut
    job_id: str  # agent reply streams via /jobs/{id}/events


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
