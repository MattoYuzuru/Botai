"""Per-user limits of dictation: requests per hour, seconds of audio per day, parallel requests.

The provider's free tier is shared by every student of the pool, so one person must not be able to
use it up. The windows slide (an hour and a day back from now). State lives in the process, like
the cooldown of the public search: a restart of the API clears it, and several API processes would
each count on their own. That is enough for a closed pool behind one API container; the numbers
are configuration (`STT_REQUESTS_PER_HOUR`, `STT_SECONDS_PER_DAY`).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

HOUR_S = 3600.0
DAY_S = 24 * 3600.0
MAX_PARALLEL = 2

Scope = Literal["hour", "day", "busy"]


class QuotaRefused(Exception):
    """The user is over a limit. `retry_after_s` is when the oldest use leaves the window."""

    def __init__(self, scope: Scope, retry_after_s: float) -> None:
        super().__init__(scope)
        self.scope: Scope = scope
        self.retry_after_s = max(1.0, retry_after_s)


@dataclass(slots=True)
class _Use:
    at: float
    seconds: float


class Slot:
    """One reserved request. The reservation is the estimate; `settle` replaces it with the length
    the provider measured, `refund` gives the seconds back after a plain refusal."""

    def __init__(self, use: _Use) -> None:
        self._use = use

    def settle(self, seconds: float) -> None:
        self._use.seconds = max(0.0, seconds)

    def refund(self) -> None:
        self._use.seconds = 0.0


class SpeechQuota:
    def __init__(
        self,
        *,
        per_hour: int,
        seconds_per_day: int,
        parallel: int = MAX_PARALLEL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.per_hour = per_hour
        self.seconds_per_day = seconds_per_day
        self.parallel = parallel
        self._now = clock
        self._uses: defaultdict[str, deque[_Use]] = defaultdict(deque)
        self._running: defaultdict[str, int] = defaultdict(int)

    def _prune(self, user_id: str, now: float) -> deque[_Use]:
        uses = self._uses[user_id]
        while uses and uses[0].at <= now - DAY_S:
            uses.popleft()
        return uses

    def _check(self, user_id: str, seconds: float, now: float) -> deque[_Use]:
        uses = self._prune(user_id, now)
        if self._running[user_id] >= self.parallel:
            raise QuotaRefused("busy", 2.0)
        in_hour = [use for use in uses if use.at > now - HOUR_S]
        if len(in_hour) >= self.per_hour:
            raise QuotaRefused("hour", in_hour[0].at + HOUR_S - now)
        spent = sum(use.seconds for use in uses)
        if spent + seconds > self.seconds_per_day:
            # The day frees up as the oldest uses age out: find when enough of them have.
            over = spent + seconds - self.seconds_per_day
            freed = 0.0
            for use in uses:
                freed += use.seconds
                if freed >= over:
                    raise QuotaRefused("day", use.at + DAY_S - now)
            raise QuotaRefused("day", DAY_S)
        return uses

    @contextmanager
    def slot(self, user_id: str, seconds: float) -> Generator[Slot]:
        """Reserve one request of `seconds` of audio, or raise `QuotaRefused`.

        The request counts against the hour whatever happens, and so do its seconds: a timeout, a
        dropped connection or a cancelled request may have been processed by the provider already.
        Only the caller can give them back (`Slot.refund`), after the provider plainly refused."""
        now = self._now()
        uses = self._check(user_id, seconds, now)
        use = _Use(now, seconds)
        uses.append(use)
        self._running[user_id] += 1
        try:
            yield Slot(use)
        finally:
            self._running[user_id] -= 1
            if self._running[user_id] <= 0:
                del self._running[user_id]
