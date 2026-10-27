from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import LlmUsageRow, User
from botai.settings import Settings
from botai.usage.service import check_quota

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


@pytest.mark.db
async def test_pool_budget_blocks_everyone(db: AsyncSession) -> None:
    spender = User(email="a@pool.test", display_name="A", password_hash="x")
    other = User(email="b@pool.test", display_name="B", password_hash="x")
    db.add_all([spender, other])
    await db.flush()
    db.add(
        LlmUsageRow(
            user_id=spender.id,
            purpose="chat",
            model="deepseek-flash",
            input_cache_miss=0,
            input_cache_hit=0,
            output=0,
            cost_usd=3.5,
            created_at=NOW,
        )
    )
    await db.flush()
    settings = Settings(quota_pool_usd_per_day=3.0)
    with pytest.raises(BotaiError) as exc:
        await check_quota(db, other.id, settings=settings, now=NOW)
    assert exc.value.code == ErrorCode.QUOTA_EXCEEDED
    assert exc.value.extra["scope"] == "pool"
    await check_quota(db, other.id, settings=Settings(quota_pool_usd_per_day=0), now=NOW)
