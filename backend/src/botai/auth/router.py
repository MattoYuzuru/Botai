"""Owner: W1. Invite-only registration, cookie sessions."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from botai.api.deps import ClockDep, SettingsDep
from botai.auth import service
from botai.auth.deps import AuthUser, OwnerUser
from botai.auth.guard import LoginGuard
from botai.auth.schemas import (
    InviteCreated,
    LoginRequest,
    MeOut,
    QuotaOut,
    RegisterRequest,
    UserOut,
)
from botai.core.clock import Clock
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import User
from botai.db.session import DbSession
from botai.settings import Settings
from botai.usage.service import quota_snapshot

LOGIN_GUARD = LoginGuard()

router = APIRouter(prefix="/api/v1", tags=["auth"])


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.env != "dev",
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        samesite="lax",
        secure=settings.env != "dev",
        path="/",
    )


async def _me(session: AsyncSession, user: User, settings: Settings, clock: Clock) -> MeOut:
    quota = await quota_snapshot(session, user.id, settings, clock.now())
    return MeOut(
        user=UserOut(
            id=user.id, email=user.email, display_name=user.display_name, is_owner=user.is_owner
        ),
        quota=QuotaOut(
            tokens_today=quota.tokens_today,
            tokens_limit=quota.tokens_limit,
            resets_at=quota.resets_at,
        ),
    )


@router.post("/auth/register", response_model=MeOut, status_code=201)
async def register(
    body: RegisterRequest,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    clock: ClockDep,
) -> MeOut:
    now = clock.now()
    user = await service.register(
        session,
        invite_code=body.invite_code,
        email=body.email,
        display_name=body.display_name,
        password=body.password.get_secret_value(),
        now=now,
    )
    token = await service.open_session(
        session, user_id=user.id, now=now, ttl_days=settings.session_ttl_days
    )
    _set_session_cookie(response, settings, token)
    return await _me(session, user, settings, clock)


@router.post("/auth/login", response_model=MeOut)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    clock: ClockDep,
) -> MeOut:
    key = (request.client.host if request.client else "?", str(body.email).lower())
    LOGIN_GUARD.check(key, now=clock.now(), limit=settings.login_failures_per_window)
    try:
        user = await service.authenticate(
            session, email=body.email, password=body.password.get_secret_value()
        )
    except BotaiError:
        LOGIN_GUARD.fail(key, now=clock.now())
        raise
    LOGIN_GUARD.reset(key)
    token = await service.open_session(
        session, user_id=user.id, now=clock.now(), ttl_days=settings.session_ttl_days
    )
    _set_session_cookie(response, settings, token)
    return await _me(session, user, settings, clock)


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request, response: Response, session: DbSession, settings: SettingsDep
) -> None:
    """Idempotent: ends the session if there is one and always clears the cookie."""
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await service.close_session(session, token)
    _clear_session_cookie(response, settings)


@router.post("/auth/invites", response_model=InviteCreated, status_code=201)
async def create_invite(owner: OwnerUser, session: DbSession, clock: ClockDep) -> InviteCreated:
    code, expires_at = await service.create_invite(session, created_by=owner.id, now=clock.now())
    return InviteCreated(code=code, expires_at=expires_at)


@router.get("/me", response_model=MeOut)
async def me(user: AuthUser, session: DbSession, settings: SettingsDep, clock: ClockDep) -> MeOut:
    row = await session.get(User, user.id)
    if row is None:  # deleted between the cookie check and now
        raise BotaiError(ErrorCode.UNAUTHORIZED, "Нужно войти")
    return await _me(session, row, settings, clock)
