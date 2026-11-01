"""When the next background sync of a v4 source is due (phase 3.3, plan §3.3).

A source is a live record of the agent: after a successful sync the app plans the next one, and the
worker scheduler replays the saved playbook without a model (`SourceRunner.sync`). This module owns
only the arithmetic — the interval, the failure backoff and the source states that are planned at
all — so the runner, the scheduler and their tests share one rule instead of three.

Honest boundaries: planning is an optimisation of freshness and cost, not a second engine (D-002);
the owner/agent can always sync manually, and `next_sync_at` only says when the next *planned* run
is due. A failure is retried with an exponential backoff capped at a few hours, and the counter
lives in `source.last_error["sync_failures"]` (JSONB, no extra column): a successful retry clears
`last_error` and with it the counter, so the backoff never sticks forever.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from botai.settings import get_settings

DEFAULT_SYNC_INTERVAL_S = 21_600.0  # 6h: the same default as BOTAI_SOURCE_SYNC_INTERVAL_S
SYNC_RETRY_BASE = timedelta(minutes=15)
SYNC_RETRY_CAP = timedelta(hours=6)
MAX_FAILURE_COUNT = 32  # guards the exponent; the cap above ends the growth far earlier

# The source states whose next sync is planned. Disabled/archived are never planned, unfinished
# setup (draft/probing/learning/needs_link) is the chatbot's job, `waiting_user` waits for a person.
PLANNED_STATUSES: tuple[str, ...] = ("active", "stale", "error", "needs_reauth")
UNPLANNED_STATUSES: frozenset[str] = frozenset({"disabled", "archived"})


def is_planned_status(status: str) -> bool:
    """Whether a source in this state may be synced by the background scheduler."""
    return status in PLANNED_STATUSES


def sync_interval_s(*, override: float | None = None) -> float:
    """The interval between planned syncs (settings, with an explicit override for a caller/test)."""
    if override is not None:
        return max(float(override), 1.0)
    try:
        configured = get_settings().source_sync_interval_s
    except Exception:  # a settings error must not stop a sync; the documented default stands
        return DEFAULT_SYNC_INTERVAL_S
    return float(max(int(configured), 1))


def failures_of(last_error: Mapping[str, Any] | None) -> int:
    """How many syncs failed in a row before now (0 when the last run succeeded/never ran)."""
    if not last_error:
        return 0
    raw = last_error.get("sync_failures")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(0, min(raw, MAX_FAILURE_COUNT))


def next_sync_at(
    now: datetime,
    *,
    ok: bool,
    failures: int,
    interval_s: float | None = None,
) -> datetime:
    """When to plan the next sync after `now`: the interval on success, backoff on failure."""
    if ok:
        return now + timedelta(seconds=sync_interval_s(override=interval_s))
    attempt = min(max(failures, 1), MAX_FAILURE_COUNT)
    delay = SYNC_RETRY_BASE * (2 ** (attempt - 1))
    return now + min(delay, SYNC_RETRY_CAP)


def backoff_for(failures: int) -> timedelta:
    """The retry delay after `failures` consecutive failures (15 min, 30, 60, …, capped)."""
    attempt = min(max(failures, 1), MAX_FAILURE_COUNT)
    return min(SYNC_RETRY_BASE * (2 ** (attempt - 1)), SYNC_RETRY_CAP)


__all__ = [
    "DEFAULT_SYNC_INTERVAL_S",
    "MAX_FAILURE_COUNT",
    "PLANNED_STATUSES",
    "SYNC_RETRY_BASE",
    "SYNC_RETRY_CAP",
    "UNPLANNED_STATUSES",
    "backoff_for",
    "failures_of",
    "is_planned_status",
    "next_sync_at",
    "sync_interval_s",
]
