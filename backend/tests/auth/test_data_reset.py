"""FR-023: reset marker belongs to the signed-in account and preserves login."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import httpx

from botai.core.clock import FrozenClock
from botai.db.models import AuditLog
from tests.auth.support import Sm, signup


async def test_reset_marker_is_scoped_latest_and_does_not_clear_auth(
    new_client: Callable[[], httpx.AsyncClient], tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    alice, bob = new_client(), new_client()
    a = await signup(alice, tx_sessionmaker, clock)
    b = await signup(bob, tx_sessionmaker, clock)
    assert "x-botai-data-reset" not in (await alice.get("/api/v1/me")).headers
    old = clock.now() - timedelta(days=1)
    async with tx_sessionmaker() as db:
        db.add_all(
            [
                AuditLog(user_id=a.id, kind="product_data_reset", detail={}, created_at=old),
                AuditLog(
                    user_id=a.id, kind="product_data_reset", detail={}, created_at=clock.now()
                ),
                AuditLog(user_id=b.id, kind="unrelated", detail={}, created_at=clock.now()),
            ]
        )
        await db.commit()
    for _ in range(2):
        response = await alice.get("/api/v1/me")
        assert response.status_code == 200
        assert response.json()["user"]["id"] == a.id
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-botai-data-reset"] == str(int(clock.now().timestamp() * 1000))
        assert "set-cookie" not in response.headers
    assert "x-botai-data-reset" not in (await bob.get("/api/v1/me")).headers
