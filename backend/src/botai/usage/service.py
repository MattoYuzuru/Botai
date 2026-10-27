"""Owner: W1. LLM usage accounting and quotas. W5's provider calls these around every request."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Date, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.clock import SystemClock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.llm import LlmUsage
from botai.db.models import LlmUsageRow, User
from botai.settings import Settings, get_settings

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


def is_peak(at: datetime) -> bool:
    """DeepSeek peak tariff: Mon-Fri 01:00-04:00 and 06:00-10:00 UTC (feasibility.md §3.1)."""
    at = at.astimezone(UTC)
    return at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def check_quota(
    session: AsyncSession,
    user_id: str,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> None:
    """Raises BotaiError(quota_exceeded, 429) when the user's daily token budget or per-minute
    request rate is used up. The problem body carries `resetsAt` (when to try again), `scope`
    (`daily` / `minute`), `limit` and `used`. Called before every model request (NFR-008)."""
    settings = settings or get_settings()
    now = now or SystemClock().now()

    used = await tokens_used_today(session, user_id, now)
    if used >= settings.quota_tokens_per_day:
        resets_at = day_bounds(now)[1]
        raise BotaiError(
            ErrorCode.QUOTA_EXCEEDED,
            "Суточный лимит модели исчерпан. Он обновится в полночь по Москве.",
            extra={
                "scope": "daily",
                "limit": settings.quota_tokens_per_day,
                "used": used,
                "resetsAt": _iso(resets_at),
            },
        )

    window_start = now - timedelta(minutes=1)
    count, oldest = (
        await session.execute(
            select(func.count(), func.min(LlmUsageRow.created_at)).where(
                LlmUsageRow.user_id == user_id,
                LlmUsageRow.created_at > window_start,
                LlmUsageRow.created_at <= now,
            )
        )
    ).one()
    if count and count >= settings.quota_requests_per_minute:
        resets_at = oldest + timedelta(minutes=1)  # the oldest request leaves the window
        wait = max(1, math.ceil((resets_at - now).total_seconds()))
        raise BotaiError(
            ErrorCode.QUOTA_EXCEEDED,
            f"Слишком много запросов к модели подряд. Подождите {wait} с.",
            extra={
                "scope": "minute",
                "limit": settings.quota_requests_per_minute,
                "used": int(count),
                "resetsAt": _iso(resets_at),
            },
        )


async def record_usage(
    session: AsyncSession,
    *,
    user_id: str,
    job_id: str | None,
    purpose: str,
    usage: LlmUsage,
    peak: bool,
    now: datetime | None = None,
) -> None:
    """One `llm_usage` row per model request, priced by `cost_usd` (peak doubles it). Counters
    only: never the prompt, the answer or anything of the student's. Commits, so the spend is not
    lost with the caller's transaction (it is already paid for)."""
    session.add(
        LlmUsageRow(
            id=new_id(),
            user_id=user_id,
            job_id=job_id,
            purpose=purpose[:12],
            model=(usage.model or "unknown")[:60],
            input_cache_miss=max(0, usage.input_cache_miss),
            input_cache_hit=max(0, usage.input_cache_hit),
            output=max(0, usage.output),
            cost_usd=round(cost_usd(usage, peak=peak), 6),
            latency_ms=max(0, usage.latency_ms),
            created_at=now or SystemClock().now(),
        )
    )
    await session.commit()


# --- reports ------------------------------------------------------------------------------------

_TOKENS = LlmUsageRow.input_cache_miss + LlmUsageRow.input_cache_hit + LlmUsageRow.output
_LOCAL_DAY = cast(func.timezone(literal(QUOTA_TZ.key), LlmUsageRow.created_at), Date)


@dataclass(frozen=True, slots=True)
class DayUsage:
    day: date
    tokens: int
    cost_usd: float


async def daily_usage(
    session: AsyncSession, user_id: str, now: datetime, *, days: int = 7
) -> list[DayUsage]:
    """The last `days` quota days, oldest first, the quiet ones as zeros."""
    today = now.astimezone(QUOTA_TZ).date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, datetime.min.time(), tzinfo=QUOTA_TZ)
    rows = (
        await session.execute(
            select(_LOCAL_DAY, func.sum(_TOKENS), func.sum(LlmUsageRow.cost_usd))
            .where(LlmUsageRow.user_id == user_id, LlmUsageRow.created_at >= start)
            .group_by(_LOCAL_DAY)
        )
    ).all()
    by_day = {d: (int(t or 0), float(c or 0)) for d, t, c in rows}
    return [
        DayUsage(first + timedelta(days=i), *by_day.get(first + timedelta(days=i), (0, 0.0)))
        for i in range(days)
    ]


@dataclass(frozen=True, slots=True)
class PoolUser:
    user_id: str
    email: str
    display_name: str
    tokens_today: int
    cost_usd_today: float
    tokens_week: int
    cost_usd_week: float


async def pool_usage(session: AsyncSession, now: datetime) -> list[PoolUser]:
    """Spend of every user of the pool (FR-024: the owner sees who uses the shared key)."""
    today = now.astimezone(QUOTA_TZ).date()
    start = datetime.combine(today - timedelta(days=6), datetime.min.time(), tzinfo=QUOTA_TZ)
    rows = (
        await session.execute(
            select(
                LlmUsageRow.user_id, _LOCAL_DAY, func.sum(_TOKENS), func.sum(LlmUsageRow.cost_usd)
            )
            .where(LlmUsageRow.created_at >= start)
            .group_by(LlmUsageRow.user_id, _LOCAL_DAY)
        )
    ).all()
    spend: dict[str, dict[date, tuple[int, float]]] = {}
    for user_id, day, tokens, cost in rows:
        spend.setdefault(user_id, {})[day] = (int(tokens or 0), float(cost or 0))
    users = (await session.execute(select(User).order_by(User.created_at, User.id))).scalars().all()
    out: list[PoolUser] = []
    for u in users:
        days = spend.get(u.id, {})
        t_today, c_today = days.get(today, (0, 0.0))
        out.append(
            PoolUser(
                u.id,
                u.email,
                u.display_name,
                t_today,
                c_today,
                sum(t for t, _ in days.values()),
                round(sum(c for _, c in days.values()), 6),
            )
        )
    return out
