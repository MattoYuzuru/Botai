from __future__ import annotations

from datetime import datetime

from botai.core.api import ApiModel


class LoginSessionOut(ApiModel):
    """Fallback remote screen (ADR-0001 §3). The main path is form mirroring via job blocks."""

    session_id: str
    ws_url: str
    ticket: str
    expires_at: datetime
