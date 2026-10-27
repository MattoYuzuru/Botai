"""Password hashing (argon2id) and opaque tokens. Nothing here touches the database."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# argon2-cffi defaults: argon2id with the RFC 9106 low-memory profile. Tests swap in a cheap one.
_hasher = PasswordHasher()

TOKEN_BYTES = 32
INVITE_BYTES = 18


def hash_password_sync(password: str) -> str:
    return _hasher.hash(password)


def verify_password_sync(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    """argon2 is CPU-bound (tens of ms): keep it off the event loop."""
    return await asyncio.to_thread(hash_password_sync, password)


async def verify_password(password_hash: str, password: str) -> bool:
    return await asyncio.to_thread(verify_password_sync, password_hash, password)


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@lru_cache
def _dummy_hash() -> str:
    return hash_password_sync(secrets.token_urlsafe(16))


async def burn_verification(password: str) -> None:
    """Spend the time of a real check, so an unknown e-mail is not distinguishable by latency."""
    await verify_password(_dummy_hash(), password)


def new_token() -> str:
    """32 random bytes, URL-safe. Only its sha256 is stored."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def new_invite_code() -> str:
    return secrets.token_urlsafe(INVITE_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
