from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from botai.core.api import ApiModel


class DocumentFromUrl(ApiModel):
    url: str = Field(max_length=2000)
    title: str | None = Field(default=None, max_length=300)
    note: str | None = Field(
        default=None, max_length=1000, description="Что это за документ, словами студента"
    )


class DocumentOut(ApiModel):
    id: str
    space_id: str
    connection_id: str | None
    kind: Literal["file", "sheet", "page"]
    title: str
    url: str | None
    mime: str | None
    fetched_at: datetime


class DocumentStarted(ApiModel):
    document_id: str | None
    connection_id: str
    job_id: str
