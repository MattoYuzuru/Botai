"""Owner: W1. LLM usage accounting and quotas. W5's provider calls these around every request."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.llm import LlmUsage

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
