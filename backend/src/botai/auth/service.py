"""Invite-only registration and cookie sessions (FR-001, security.md).

Invite codes and session tokens are shown once; the database keeps only their sha256. Every
function takes the caller's `AsyncSession` and commits itself, so a failed step leaves nothing
behind.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from botai.auth import security
from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import AuthSession, Invite, User

INVITE_TTL = timedelta(days=7)

_INVALID_INVITE = "Приглашение недействительно, истекло или уже использовано"
_BAD_CREDENTIALS = "Неверная почта или пароль"


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def create_invite(
    session: AsyncSession, *, created_by: str, now: datetime, ttl: timedelta = INVITE_TTL
) -> tuple[str, datetime]:
    """Returns the plain code (the only time it exists) and its expiry."""
    code = security.new_invite_code()
    expires_at = now + ttl
    session.add(
        Invite(code_hash=security.token_hash(code), created_by=created_by, expires_at=expires_at)
    )
    await session.commit()
    return code, expires_at


async def register(
    session: AsyncSession,
    *,
    invite_code: str,
    email: str,
    display_name: str,
    password: str,
    now: datetime,
) -> User:
    code_hash = security.token_hash(invite_code.strip())
    # Invite first: without a valid one nothing else about the pool (e.g. which e-mails are
    # registered) is revealed.
    invite_id = await session.scalar(
        select(Invite.id).where(
            Invite.code_hash == code_hash, Invite.used_by.is_(None), Invite.expires_at > now
        )
    )
    if invite_id is None:
        raise BotaiError(ErrorCode.FORBIDDEN, _INVALID_INVITE)

    user = User(
        email=normalize_email(email),
        display_name=display_name.strip(),
        password_hash=await security.hash_password(password),
        is_owner=False,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise BotaiError(ErrorCode.CONFLICT, "Эта почта уже зарегистрирована") from None

    # Single use under concurrency: the second UPDATE waits for the first and then matches no row.
    redeemed = await session.execute(
        update(Invite)
        .where(Invite.id == invite_id, Invite.used_by.is_(None), Invite.expires_at > now)
        .values(used_by=user.id)
        .returning(Invite.id)
    )
    if redeemed.first() is None:
        await session.rollback()
        raise BotaiError(ErrorCode.FORBIDDEN, _INVALID_INVITE)
    await session.commit()
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    """Checks the BotAI password. Unknown e-mail, wrong password and disabled account look alike."""
    user = await session.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None or user.disabled:
        await security.burn_verification(password)
        raise BotaiError(ErrorCode.UNAUTHORIZED, _BAD_CREDENTIALS)
    if not await security.verify_password(user.password_hash, password):
        raise BotaiError(ErrorCode.UNAUTHORIZED, _BAD_CREDENTIALS)
    if security.needs_rehash(user.password_hash):
        user.password_hash = await security.hash_password(password)
        await session.commit()
    return user


async def open_session(session: AsyncSession, *, user_id: str, now: datetime, ttl_days: int) -> str:
    """Creates a session and returns the cookie token (stored only as sha256)."""
    token = security.new_token()
    await session.execute(
        delete(AuthSession).where(AuthSession.user_id == user_id, AuthSession.expires_at <= now)
    )
    session.add(
        AuthSession(
            user_id=user_id,
            token_hash=security.token_hash(token),
            expires_at=now + timedelta(days=ttl_days),
        )
    )
    await session.commit()
    return token


async def close_session(session: AsyncSession, token: str) -> None:
    await session.execute(
        delete(AuthSession).where(AuthSession.token_hash == security.token_hash(token))
    )
    await session.commit()


async def bootstrap_owner(
    session: AsyncSession, *, email: str, display_name: str, password: str
) -> User:
    """First user of the pool. Refuses to run twice: further people join by invite."""
    if await session.scalar(select(User.id).where(User.is_owner.is_(True)).limit(1)):
        raise BotaiError(ErrorCode.CONFLICT, "Владелец уже создан")
    user = User(
        email=normalize_email(email),
        display_name=display_name.strip(),
        password_hash=await security.hash_password(password),
        is_owner=True,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise BotaiError(ErrorCode.CONFLICT, "Эта почта уже зарегистрирована") from None
    return user
