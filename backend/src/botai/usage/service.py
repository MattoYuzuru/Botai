"""Owner: W1. LLM usage accounting and quotas. W5's provider calls these around every request."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.llm import LlmUsage
from botai.db.models import LlmUsageRow
from botai.settings import Settings

# The daily budget resets at local midnight of the pool (owner and friends are in Russia).
QUOTA_TZ = ZoneInfo("Europe/Moscow")

# USD per 1M tokens, off-peak (docs/research/feasibility.md §3.1). Peak = ×2.
PRICES: dict[str, tuple[float, float, float]] = {
    # model: (input cache hit, input cache miss, output)
    "deepseek-flash": (0.003, 0.15, 0.60),
    "deepseek-v4-pro": (0.022, 0.66, 1.98),
}


def cost_usd(usage: LlmUsage, *, peak: bool) -> float:
    hit, miss, out = PRICES.get(usage.model, PRICES["deepseek-flash"])
    k = 2.0 if peak else 1.0
    return (
        k
        * (usage.input_cache_hit * hit + usage.input_cache_miss * miss + usage.output * out)
        / 1_000_000
    )


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    tokens_today: int
    tokens_limit: int
    resets_at: datetime


def day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the quota day that contains `now`."""
    local = now.astimezone(QUOTA_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


async def tokens_used_today(session: AsyncSession, user_id: str, now: datetime) -> int:
    start, end = day_bounds(now)
    total = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    LlmUsageRow.input_cache_miss + LlmUsageRow.input_cache_hit + LlmUsageRow.output
                ),
                0,
            )
        ).where(
            LlmUsageRow.user_id == user_id,
            LlmUsageRow.created_at >= start,
            LlmUsageRow.created_at < end,
        )
    )
    return int(total or 0)


async def quota_snapshot(
    session: AsyncSession, user_id: str, settings: Settings, now: datetime
) -> QuotaSnapshot:
    return QuotaSnapshot(
        tokens_today=await tokens_used_today(session, user_id, now),
        tokens_limit=settings.quota_tokens_per_day,
        resets_at=day_bounds(now)[1],
    )


async def check_quota(session: AsyncSession, user_id: str) -> None:
    """Raises BotaiError(quota_exceeded) if the user's daily token budget or rate is exhausted."""
    raise NotImplementedError("W1")


async def record_usage(
    session: AsyncSession,
    *,
    user_id: str,
    job_id: str | None,
    purpose: str,
    usage: LlmUsage,
    peak: bool,
) -> None:
    raise NotImplementedError("W1")
