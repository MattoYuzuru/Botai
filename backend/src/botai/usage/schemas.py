from __future__ import annotations

from datetime import datetime

from botai.core.api import ApiModel


class UsageDay(ApiModel):
    day: str
    tokens: int
    cost_usd: float


class UsageOut(ApiModel):
    tokens_today: int
    tokens_limit: int
    resets_at: datetime
    last_days: list[UsageDay]
