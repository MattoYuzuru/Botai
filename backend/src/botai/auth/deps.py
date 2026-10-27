"""Owner: W1. FastAPI dependencies used by every module's router. Keep the signatures.

Tests of other modules override them: `app.dependency_overrides[current_user] = lambda: user`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.api.deps import get_clock
from botai.auth import security
from botai.core.clock import Clock
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import AuthSession, User
from botai.db.session import db_session
from botai.settings import Settings, get_settings


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: str
    email: str
    is_owner: bool


async def resolve_session(session: AsyncSession, token: str, now: datetime) -> CurrentUser | None:
    """The user a live session token belongs to; None for unknown, expired or disabled."""
    row = (
        await session.execute(
            select(User.id, User.email, User.is_owner)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(
                AuthSession.token_hash == security.token_hash(token),
                AuthSession.expires_at > now,
                User.disabled.is_(False),
            )
        )
    ).first()
    if row is None:
        return None
    return CurrentUser(id=row.id, email=row.email, is_owner=row.is_owner)


async def current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> CurrentUser:
    """Resolves the session cookie. Raises 401 `unauthorized` when absent or expired."""
    token = request.cookies.get(settings.session_cookie_name)
    user = await resolve_session(session, token, clock.now()) if token else None
    if user is None:
        raise BotaiError(ErrorCode.UNAUTHORIZED, "Нужно войти")
    return user


async def owner_user(user: Annotated[CurrentUser, Depends(current_user)]) -> CurrentUser:
    if not user.is_owner:
        raise BotaiError(ErrorCode.FORBIDDEN, "Только для владельца")
    return user


AuthUser = Annotated[CurrentUser, Depends(current_user)]
OwnerUser = Annotated[CurrentUser, Depends(owner_user)]
