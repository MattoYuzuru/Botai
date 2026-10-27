"""Owner: W1. FastAPI dependencies used by every module's router. Keep the signatures.

Tests of other modules override them: `app.dependency_overrides[current_user] = lambda: user`.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from botai.core.errors import BotaiError, ErrorCode


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: str
    email: str
    is_owner: bool


async def current_user(request: Request) -> CurrentUser:
    """Resolves the session cookie. Raises 401 `unauthorized` when absent or expired."""
    raise BotaiError(ErrorCode.UNAUTHORIZED, "Нужно войти")


async def owner_user(request: Request) -> CurrentUser:
    raise BotaiError(ErrorCode.FORBIDDEN, "Только для владельца")
