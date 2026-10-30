from __future__ import annotations

from datetime import datetime
from typing import Literal

from botai.core.api import ApiModel

IngestState = Literal["none", "queued", "indexed", "failed"]


class AttachmentOut(ApiModel):
    id: str
    name: str
    mime: str
    size: int
    kind: Literal["image", "document", "audio", "other"]
    created_at: datetime
    document_id: str | None
    ingest: IngestState  # a document attachment: is its text searchable yet
