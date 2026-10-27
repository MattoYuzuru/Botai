"""Owner: W1. Token usage and quota of the current user, and the owner's view of the pool."""

from __future__ import annotations

from fastapi import APIRouter

from botai.api.deps import ClockDep, SettingsDep
from botai.auth.deps import AuthUser, OwnerUser
from botai.db.session import DbSession
from botai.usage import service
from botai.usage.schemas import PoolUsageOut, PoolUserUsage, UsageDay, UsageOut

router = APIRouter(prefix="/api/v1", tags=["usage"])


@router.get("/usage", response_model=UsageOut)
async def usage(
    user: AuthUser, session: DbSession, settings: SettingsDep, clock: ClockDep
) -> UsageOut:
    """Tokens spent today against the daily budget, and the last seven days (quota days run
    from local midnight in Moscow)."""
    now = clock.now()
    quota = await service.quota_snapshot(session, user.id, settings, now)
    days = await service.daily_usage(session, user.id, now)
    return UsageOut(
        tokens_today=quota.tokens_today,
        tokens_limit=quota.tokens_limit,
        resets_at=quota.resets_at,
        last_days=[
            UsageDay(day=d.day.isoformat(), tokens=d.tokens, cost_usd=d.cost_usd) for d in days
        ],
    )


@router.get("/usage/pool", response_model=PoolUsageOut)
async def pool_usage(
    owner: OwnerUser, session: DbSession, settings: SettingsDep, clock: ClockDep
) -> PoolUsageOut:
    """Owner only: the spend of every user of the shared model key (FR-024)."""
    now = clock.now()
    rows = await service.pool_usage(session, now)
    return PoolUsageOut(
        tokens_limit=settings.quota_tokens_per_day,
        resets_at=service.day_bounds(now)[1],
        users=[
            PoolUserUsage(
                user_id=r.user_id,
                email=r.email,
                display_name=r.display_name,
                tokens_today=r.tokens_today,
                cost_usd_today=r.cost_usd_today,
                tokens_week=r.tokens_week,
                cost_usd_week=r.cost_usd_week,
            )
            for r in rows
        ],
    )
