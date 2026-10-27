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


class PoolUserUsage(ApiModel):
    user_id: str
    email: str
    display_name: str
    tokens_today: int
    cost_usd_today: float
    tokens_week: int
    cost_usd_week: float


class PoolUsageOut(ApiModel):
    """Owner's view of the shared key: who spends what."""

    tokens_limit: int
    resets_at: datetime
    users: list[PoolUserUsage]
