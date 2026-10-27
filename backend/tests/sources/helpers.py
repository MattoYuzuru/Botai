"""Shared helpers for source and learning tests: a context bound to the in-process fakeuni."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

from botai.core.browser import SessionCookie, SessionState
from botai.core.http import RetryPolicy
from botai.core.vault import InMemoryVault, Vault
from botai.sources.adapter import ExpiredRules, SyncContext
from tests.fakeuni import data
from tests.fakeuni.login import COOKIE, SESSIONS

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # a Tuesday of the fakeuni week
USER_ID = "01J0000000000000000000USER"
CONN_ID = "01J0000000000000000000CONN"
FAST = RetryPolicy(attempts=2, backoff_s=(0.0,), jitter=0.0)
LMS_HOST = "lms.fakeuni.test"


def new_session_token(value: str = "tok-fixture-session") -> str:
    SESSIONS.add(value)
    return value


def session_state(
    token: str | None = None, *, expires_hint: datetime | None = None
) -> SessionState:
    value = token or new_session_token()
    return SessionState(
        origin=data.ORIGIN,
        cookies=[SessionCookie(name=COOKIE, value=value, domain="fakeuni.test")],  # pyright: ignore[reportArgumentType]
        captured_at=NOW,
        expires_hint=expires_hint,
    )


def make_ctx(
    app: object | None = None,
    *,
    session: SessionState | None = None,
    logged_in: bool = True,
    allow_hosts: tuple[str, ...] = ("fakeuni.test",),
    origin: str = data.ORIGIN,
    vault: Vault | None = None,
    profile: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    expired: ExpiredRules | None = None,
    **extra: Any,
) -> SyncContext:
    if transport is None:
        assert isinstance(app, FastAPI)
        transport = httpx.ASGITransport(app=app)
    if session is None and logged_in:
        session = session_state()
    return SyncContext(
        user_id=USER_ID,
        connection_id=CONN_ID,
        origin=origin,
        allow_hosts=allow_hosts,
        now=NOW,
        vault=vault or InMemoryVault(),
        tz=ZoneInfo("Europe/Moscow"),
        session=session,
        profile=profile or {},
        transport=transport,
        retry=FAST,
        expired=expired or ExpiredRules(),
        **extra,
    )
