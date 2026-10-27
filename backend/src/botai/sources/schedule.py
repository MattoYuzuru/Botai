"""When a connection is synced next and when its data counts as stale (`freshness`).

The job scheduler (W1) enqueues a sync for every active connection with `next_sync_at <= now`;
this module only decides the value of `next_sync_at`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

DEFAULT_EVERY = {"recipe": 180, "moodle_ws": 120, "ical": 180, "document": 0}
MIN_EVERY = 15
FAILURE_RETRY = timedelta(minutes=5)
RATE_LIMIT_RETRY = timedelta(minutes=15)
JITTER_SHARE = 0.1


@dataclass(frozen=True, slots=True)
class Freshness:
    every_minutes: int  # 0 = never refreshed on a timer
    stale_after_minutes: int


def freshness_of(spec: dict[str, Any]) -> Freshness:
    adapter = str(spec.get("adapter", "recipe"))
    every = DEFAULT_EVERY.get(adapter, 180)
    stale: int | None = None
    if adapter == "recipe":
        fresh = spec.get("recipe", {}).get("freshness") or {}
        every = int(fresh.get("syncEveryMinutes") or every)
        stale = fresh.get("staleAfterMinutes")
    elif adapter == "document":
        every = int(spec.get("source", {}).get("refreshEveryMinutes") or 0)
    if every:
        every = max(every, MIN_EVERY)
    return Freshness(every, int(stale) if stale else max(every * 4, 720))


def _jitter(connection_id: str, every_minutes: int) -> timedelta:
    """Spread connections of one origin instead of syncing them in the same second."""
    share = int(hashlib.sha256(connection_id.encode()).hexdigest()[:4], 16) / 0xFFFF
    return timedelta(minutes=every_minutes * JITTER_SHARE * share)


def next_sync_at(now: datetime, fresh: Freshness, connection_id: str) -> datetime | None:
    if fresh.every_minutes == 0:
        return None
    return (
        now + timedelta(minutes=fresh.every_minutes) + _jitter(connection_id, fresh.every_minutes)
    )


def is_stale(now: datetime, last_sync_at: datetime | None, fresh: Freshness) -> bool:
    if last_sync_at is None:
        return False
    return now - last_sync_at > timedelta(minutes=fresh.stale_after_minutes)
