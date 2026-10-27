"""Brute-force guard for BotAI's own login: failed attempts per (client, e-mail) in a window.

In memory on purpose: one API process on one server. A restart forgets counters, which is fine.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

from botai.core.errors import BotaiError, ErrorCode

WINDOW = timedelta(minutes=15)
Key = tuple[str, str]


class LoginGuard:
    def __init__(self) -> None:
        self._fails: dict[Key, deque[datetime]] = {}

    def _recent(self, key: Key, now: datetime) -> deque[datetime]:
        q = self._fails.setdefault(key, deque())
        while q and now - q[0] > WINDOW:
            q.popleft()
        return q

    def check(self, key: Key, *, now: datetime, limit: int) -> None:
        q = self._recent(key, now)
        if len(q) >= limit:
            wait = int((q[0] + WINDOW - now).total_seconds()) + 1
            raise BotaiError(
                ErrorCode.TOO_MANY_ATTEMPTS,
                "Слишком много неудачных попыток входа. Попробуйте позже.",
                extra={"retryAfterSeconds": wait},
            )

    def fail(self, key: Key, *, now: datetime) -> None:
        self._recent(key, now).append(now)

    def reset(self, key: Key) -> None:
        self._fails.pop(key, None)
