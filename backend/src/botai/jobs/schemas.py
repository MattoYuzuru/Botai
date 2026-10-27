from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from botai.core.api import ApiModel

JobStateOut = Literal["queued", "running", "waiting_user", "done", "failed", "cancelled"]


class JobOut(ApiModel):
    id: str
    kind: str
    state: JobStateOut
    stage: str | None
    connection_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class AnswerIn(ApiModel):
    """Answer to an AgentUiBlock. For secret inputs the API seals `values` into the Vault
    (kind=transient) and stores nothing else."""

    block_id: str = Field(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")
    values: list[str] | str | bool | None = None
