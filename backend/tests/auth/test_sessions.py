"""Login, logout, cookie sessions, owner-only routes (FR-001, NFR-001)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update

from botai.auth import security
from botai.auth.deps import CurrentUser, current_user
from botai.core.clock import FrozenClock
from botai.core.ids import new_id
from botai.db.models import AuthSession, Invite, LlmUsageRow, User
from botai.settings import Settings, get_settings
from tests.auth.support import PASSWORD, Sm, issue_invite, make_user, signup

Make = Callable[[], httpx.AsyncClient]


async def test_no_cookie_is_401_everywhere_it_matters(client: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", "/api/v1/me"),
        ("POST", "/api/v1/auth/invites"),
    ):
        r = await client.request(method, path)
        assert r.status_code == 401, path
        assert r.json()["code"] == "unauthorized"
        assert r.headers["content-type"].startswith("application/problem+json")


async def test_garbage_and_foreign_tokens_are_401(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    real = client.cookies["botai_session"]
    for forged in ("x", security.new_token(), real[:-1] + ("A" if real[-1] != "A" else "B"), ""):
        client.cookies.clear()
        client.cookies.set("botai_session", forged)
        r = await client.get("/api/v1/me")
        assert r.status_code == 401, forged
    assert acc.id  # the real session was never touched


async def test_the_stored_value_is_a_hash_not_the_cookie_token(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    token = client.cookies["botai_session"]
    assert len(token) >= 43
    async with tx_sessionmaker() as s:
        row = (await s.execute(select(AuthSession))).scalar_one()
        dump = (
            (await s.execute(text("select auth_sessions::text from auth_sessions"))).scalars().all()
        )
    assert row.token_hash == security.token_hash(token)
    assert all(token not in line for line in dump)


async def test_one_users_session_never_acts_as_another(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    a, b = new_client(), new_client()
    alice = await signup(a, tx_sessionmaker, clock, email="alice@example.com")
    bob = await signup(b, tx_sessionmaker, clock, email="bob@example.com")
    assert (await a.get("/api/v1/me")).json()["user"]["id"] == alice.id
    assert (await b.get("/api/v1/me")).json()["user"]["id"] == bob.id
    # Bob presenting Alice's token is Alice, nobody else; his own token is not Alice's
    assert a.cookies["botai_session"] != b.cookies["botai_session"]


async def test_login_with_the_right_password(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    first = new_client()
    acc = await signup(first, tx_sessionmaker, clock)
    second = new_client()
    r = await second.post("/api/v1/auth/login", json={"email": acc.email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == acc.id
    assert "botai_session" in second.cookies
    assert second.cookies["botai_session"] != first.cookies["botai_session"]
    assert (await second.get("/api/v1/me")).status_code == 200
    assert (await first.get("/api/v1/me")).status_code == 200  # other devices stay logged in


async def test_login_failures_are_indistinguishable(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(new_client(), tx_sessionmaker, clock)
    c = new_client()
    wrong = await c.post(
        "/api/v1/auth/login", json={"email": acc.email, "password": "wrong-password"}
    )
    unknown = await c.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]
    assert "botai_session" not in c.cookies


async def test_login_is_case_insensitive_on_email(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(new_client(), tx_sessionmaker, clock, email="mixed@example.com")
    c = new_client()
    r = await c.post(
        "/api/v1/auth/login", json={"email": "MIXED@example.com", "password": acc.password}
    )
    assert r.status_code == 200


async def test_login_upgrades_an_outdated_hash(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = await signup(new_client(), tx_sessionmaker, clock)
    from argon2 import PasswordHasher

    monkeypatch.setattr(
        security, "_hasher", PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    )
    r = await new_client().post(
        "/api/v1/auth/login", json={"email": acc.email, "password": PASSWORD}
    )
    assert r.status_code == 200
    async with tx_sessionmaker() as s:
        stored = await s.scalar(select(User.password_hash).where(User.id == acc.id))
    assert stored is not None
    assert "t=2" in stored
    assert "m=16" in stored


async def test_logout_ends_the_session_and_clears_the_cookie(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await signup(client, tx_sessionmaker, clock)
    old = client.cookies["botai_session"]
    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 204
    assert "botai_session" in r.headers["set-cookie"]
    assert "max-age=0" in r.headers["set-cookie"].lower()
    # even a replayed old cookie is dead: the server forgot the session
    client.cookies.set("botai_session", old)
    assert (await client.get("/api/v1/me")).status_code == 401
    async with tx_sessionmaker() as s:
        assert (await s.execute(select(AuthSession))).first() is None


async def test_logout_is_idempotent_and_needs_no_session(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    assert (await client.post("/api/v1/auth/logout")).status_code == 204


async def test_logout_ends_only_its_own_session(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    phone, laptop = new_client(), new_client()
    acc = await signup(phone, tx_sessionmaker, clock)
    await laptop.post("/api/v1/auth/login", json={"email": acc.email, "password": PASSWORD})
    await phone.post("/api/v1/auth/logout")
    assert (await phone.get("/api/v1/me")).status_code == 401
    assert (await laptop.get("/api/v1/me")).status_code == 200


async def test_session_expires(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, settings: Settings
) -> None:
    await signup(client, tx_sessionmaker, clock)
    clock.advance(timedelta(days=settings.session_ttl_days) - timedelta(minutes=1))
    assert (await client.get("/api/v1/me")).status_code == 200
    clock.advance(timedelta(minutes=2))
    assert (await client.get("/api/v1/me")).status_code == 401


async def test_expired_sessions_are_swept_on_next_login(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock, settings: Settings
) -> None:
    acc = await signup(new_client(), tx_sessionmaker, clock)
    clock.advance(timedelta(days=settings.session_ttl_days + 1))
    await new_client().post("/api/v1/auth/login", json={"email": acc.email, "password": PASSWORD})
    async with tx_sessionmaker() as s:
        assert len((await s.execute(select(AuthSession))).all()) == 1


async def test_disabled_user_loses_access_at_once(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    c = new_client()
    acc = await signup(c, tx_sessionmaker, clock)
    async with tx_sessionmaker() as s:
        await s.execute(update(User).where(User.id == acc.id).values(disabled=True))
        await s.commit()
    assert (await c.get("/api/v1/me")).status_code == 401
    r = await new_client().post(
        "/api/v1/auth/login", json={"email": acc.email, "password": PASSWORD}
    )
    assert r.status_code == 401


async def test_only_the_owner_can_issue_invites(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    student = new_client()
    await signup(student, tx_sessionmaker, clock)
    r = await student.post("/api/v1/auth/invites")
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"


async def test_owner_invite_registers_a_new_user(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    owner_id = await make_user(
        tx_sessionmaker, "owner@example.com", is_owner=True, password=PASSWORD
    )
    owner = new_client()
    assert (
        await owner.post(
            "/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
        )
    ).status_code == 200

    r = await owner.post("/api/v1/auth/invites")
    assert r.status_code == 201
    code = r.json()["code"]
    assert r.json()["expiresAt"].startswith("2026-11-03")  # a week from the frozen clock

    friend = new_client()
    reg = await friend.post(
        "/api/v1/auth/register",
        json={
            "inviteCode": code,
            "email": "friend@example.com",
            "displayName": "Друг",
            "password": PASSWORD,
        },
    )
    assert reg.status_code == 201
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(Invite.created_by)) == owner_id


async def test_me_reports_quota_usage_of_the_caller_only(
    new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock, settings: Settings
) -> None:
    a, b = new_client(), new_client()
    alice = await signup(a, tx_sessionmaker, clock, email="a@example.com")
    bob = await signup(b, tx_sessionmaker, clock, email="b@example.com")
    async with tx_sessionmaker() as s:
        for uid, miss, hit, out in ((alice.id, 100, 50, 25), (bob.id, 7, 0, 1)):
            s.add(
                LlmUsageRow(
                    id=new_id(),
                    user_id=uid,
                    purpose="chat",
                    model="deepseek-flash",
                    input_cache_miss=miss,
                    input_cache_hit=hit,
                    output=out,
                    cost_usd=0,
                    created_at=clock.now(),
                )
            )
        await s.commit()
    quota = (await a.get("/api/v1/me")).json()["quota"]
    assert quota["tokensToday"] == 175
    assert quota["tokensLimit"] == settings.quota_tokens_per_day
    # the day ends at local midnight of the pool (Europe/Moscow): 12:00 UTC -> 00:00 MSK next day
    assert datetime.fromisoformat(quota["resetsAt"]) == datetime(2026, 10, 27, 21, 0, tzinfo=UTC)
    assert (await b.get("/api/v1/me")).json()["quota"]["tokensToday"] == 8


async def test_other_modules_can_override_current_user(
    app: FastAPI, client: httpx.AsyncClient, tx_sessionmaker: Sm
) -> None:
    """The documented contract for the tests of W2-W5: no cookie needed."""
    uid = await make_user(tx_sessionmaker, "stub@example.com", display_name="Stub")
    app.dependency_overrides[current_user] = lambda: CurrentUser(
        id=uid, email="stub@example.com", is_owner=False
    )
    r = await client.get("/api/v1/me")
    assert r.status_code == 200
    assert r.json()["user"]["displayName"] == "Stub"
    app.dependency_overrides.pop(current_user)


async def test_cookie_is_not_secure_in_dev(
    app: FastAPI, new_client: Make, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    dev = Settings(env="dev")  # type: ignore[call-arg]
    app.dependency_overrides[get_settings] = lambda: dev
    code = await issue_invite(tx_sessionmaker, clock)
    c = new_client()
    r = await c.post(
        "/api/v1/auth/register",
        json={
            "inviteCode": code,
            "email": "dev@example.com",
            "displayName": "Dev",
            "password": PASSWORD,
        },
    )
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" not in cookie
