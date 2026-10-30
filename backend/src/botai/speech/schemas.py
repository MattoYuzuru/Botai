from __future__ import annotations

from pydantic import BaseModel


class TranscriptOut(BaseModel):
    """What was said, as plain text. The recording itself is not kept anywhere."""

    text: str
