from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from botai.auth.guard import LoginGuard
from botai.core.errors import BotaiError, ErrorCode

T0 = datetime(2026, 10, 27, 12, tzinfo=UTC)


def test_blocks_after_limit_and_recovers() -> None:
    guard = LoginGuard()
    key = ("1.2.3.4", "a@b.c")
    for i in range(3):
        guard.check(key, now=T0, limit=3)
        guard.fail(key, now=T0 + timedelta(seconds=i))
    with pytest.raises(BotaiError) as exc:
        guard.check(key, now=T0 + timedelta(minutes=1), limit=3)
    assert exc.value.code == ErrorCode.TOO_MANY_ATTEMPTS
    assert exc.value.status == 429
    guard.check(key, now=T0 + timedelta(minutes=16), limit=3)


def test_success_resets_and_keys_are_separate() -> None:
    guard = LoginGuard()
    a, b = ("1.1.1.1", "a@b.c"), ("1.1.1.1", "x@b.c")
    guard.fail(a, now=T0)
    guard.fail(a, now=T0)
    guard.check(b, now=T0, limit=2)
    guard.reset(a)
    guard.check(a, now=T0, limit=2)
