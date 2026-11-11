"""D-041: open registration; optional legacy invites remain single-use and hashed."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text

from botai.auth import security, service
from botai.core.clock import FrozenClock
from botai.db.models import AuthSession, Invite, User
from tests.auth.support import PASSWORD, Sm, issue_invite, make_user


def _body(code: str, email: str = "ann@example.com", password: str = PASSWORD) -> dict[str, str]:
    return {"inviteCode": code, "email": email, "displayName": "Анна", "password": password}


@pytest.mark.parametrize("with_null_invite", [False, True])
async def test_registration_without_invite_creates_active_regular_user(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, with_null_invite: bool
) -> None:
    body: dict[str, str | bool | None] = {
        "email": "Ann@Example.Com",
        "displayName": "Анна",
        "password": PASSWORD,
        "isOwner": True,
    }
    if with_null_invite:
        body["inviteCode"] = None
    response = await client.post("/api/v1/auth/register", json=body)
    assert response.status_code == 201
    user = response.json()["user"]
    assert user["email"] == "ann@example.com"
    assert user["isOwner"] is False
    assert (await client.get("/api/v1/me")).json()["user"]["id"] == user["id"]
    assert (await client.post("/api/v1/auth/invites")).status_code == 403
    async with tx_sessionmaker() as session:
        stored = await session.get(User, user["id"])
        assert stored is not None
        assert not stored.disabled
        assert not stored.is_owner
        assert await session.scalar(select(Invite.id)) is None
    await client.post("/api/v1/auth/logout")
    login = await client.post(
        "/api/v1/auth/login", json={"email": "ann@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200


async def test_open_registration_duplicate_email_conflicts(client: httpx.AsyncClient) -> None:
    body = {"email": "Ann@Example.Com", "displayName": "Анна", "password": PASSWORD}
    assert (await client.post("/api/v1/auth/register", json=body)).status_code == 201
    body["email"] = "ann@example.com"
    response = await client.post("/api/v1/auth/register", json=body)
    assert response.status_code == 409


async def test_open_registration_rejects_weak_password(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "ann@example.com", "displayName": "Анна", "password": "short"},
    )
    assert response.status_code == 422


async def test_register_with_an_invite_creates_user_and_logs_in(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    code = await issue_invite(tx_sessionmaker, clock)
    r = await client.post("/api/v1/auth/register", json=_body(code))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["email"] == "ann@example.com"
    assert body["user"]["displayName"] == "Анна"
    assert body["user"]["isOwner"] is False
    assert body["quota"]["tokensToday"] == 0
    assert body["quota"]["tokensLimit"] > 0
    assert PASSWORD not in r.text
    assert (await client.get("/api/v1/me")).json()["user"]["id"] == body["user"]["id"]


async def test_session_cookie_attributes(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    code = await issue_invite(tx_sessionmaker, clock)
    r = await client.post("/api/v1/auth/register", json=_body(code))
    cookie = r.headers["set-cookie"].lower()
    assert "botai_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" in cookie
    assert "path=/" in cookie
    assert f"max-age={30 * 86400}" in cookie


async def test_register_with_an_explicit_invalid_invite_is_refused(
    client: httpx.AsyncClient, tx_sessionmaker: Sm
) -> None:
    r = await client.post("/api/v1/auth/register", json=_body("nonexistent-code-123"))
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(User.id).where(User.email == "ann@example.com")) is None


async def test_invite_is_single_use(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    code = await issue_invite(tx_sessionmaker, clock)
    first = await client.post("/api/v1/auth/register", json=_body(code))
    assert first.status_code == 201
    second = await client.post("/api/v1/auth/register", json=_body(code, "bob@example.com"))
    assert second.status_code == 403
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(User.id).where(User.email == "bob@example.com")) is None


async def test_expired_invite_is_refused(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    code = await issue_invite(tx_sessionmaker, clock)
    clock.advance(service.INVITE_TTL + timedelta(seconds=1))
    r = await client.post("/api/v1/auth/register", json=_body(code))
    assert r.status_code == 403


async def test_invalid_and_used_invites_look_the_same(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    code = await issue_invite(tx_sessionmaker, clock)
    await client.post("/api/v1/auth/register", json=_body(code))
    used = await client.post("/api/v1/auth/register", json=_body(code, "x@example.com"))
    unknown = await client.post(
        "/api/v1/auth/register", json=_body("zzzzzzzzzzzz", "y@example.com")
    )
    assert used.json()["detail"] == unknown.json()["detail"]


async def test_invite_is_stored_only_as_sha256(tx_sessionmaker: Sm, clock: FrozenClock) -> None:
    code = await issue_invite(tx_sessionmaker, clock)
    async with tx_sessionmaker() as s:
        hashes = (await s.execute(select(Invite.code_hash))).scalars().all()
        dump = (await s.execute(text("select invites::text from invites"))).scalars().all()
    assert security.token_hash(code) in hashes
    assert all(code not in row for row in dump)


async def test_duplicate_email_conflicts_and_keeps_the_invite(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    await make_user(tx_sessionmaker, "ann@example.com")
    code = await issue_invite(tx_sessionmaker, clock)
    r = await client.post("/api/v1/auth/register", json=_body(code))
    assert r.status_code == 409
    assert r.json()["code"] == "conflict"
    # the invite was not burned by the failed attempt
    ok = await client.post("/api/v1/auth/register", json=_body(code, "ann2@example.com"))
    assert ok.status_code == 201


async def test_email_is_case_insensitive(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    code = await issue_invite(tx_sessionmaker, clock)
    await client.post("/api/v1/auth/register", json=_body(code, "Ann@Example.Com"))
    again = await client.post(
        "/api/v1/auth/register",
        json=_body(await issue_invite(tx_sessionmaker, clock), "ann@example.com"),
    )
    assert again.status_code == 409


async def test_password_is_stored_as_an_argon2_hash(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    code = await issue_invite(tx_sessionmaker, clock)
    await client.post("/api/v1/auth/register", json=_body(code))
    async with tx_sessionmaker() as s:
        stored = await s.scalar(select(User.password_hash).where(User.email == "ann@example.com"))
        dump = (await s.execute(text("select users::text from users"))).scalars().all()
    assert stored is not None
    assert stored.startswith("$argon2id$")
    assert all(PASSWORD not in row for row in dump)


async def test_weak_password_is_a_validation_error(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    code = await issue_invite(tx_sessionmaker, clock)
    r = await client.post("/api/v1/auth/register", json=_body(code, password="short"))
    assert r.status_code == 422


async def test_registration_opens_exactly_one_session(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    code = await issue_invite(tx_sessionmaker, clock)
    await client.post("/api/v1/auth/register", json=_body(code))
    async with tx_sessionmaker() as s:
        sessions = (await s.execute(select(AuthSession))).scalars().all()
    assert len(sessions) == 1
