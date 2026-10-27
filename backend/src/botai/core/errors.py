"""Machine error codes (contracts §7) and problem+json rendering."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ErrorCode(StrEnum):
    # source / sync
    SOURCE_AUTH_REQUIRED = "source_auth_required"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_RATE_LIMITED = "source_rate_limited"
    SOURCE_BOT_PROTECTION = "source_bot_protection"
    LOGIN_FAILED = "login_failed"
    RECIPE_DRIFT = "recipe_drift"
    RECIPE_REJECTED = "recipe_rejected"
    EGRESS_BLOCKED = "egress_blocked"
    # actions
    ACTION_CHANGED = "action_changed"
    ACTION_EXPIRED = "action_expired"
    # agent / quota
    QUOTA_EXCEEDED = "quota_exceeded"
    INJECTION_BLOCKED = "injection_blocked"
    LLM_UNAVAILABLE = "llm_unavailable"
    # generic API
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    UNAUTHORIZED = "unauthorized"
    VALIDATION = "validation"
    CONFLICT = "conflict"
    BLOCK_ANSWERED = "block_answered"
    NOT_IMPLEMENTED = "not_implemented"
    TOO_MANY_ATTEMPTS = "too_many_attempts"
    INTERNAL = "internal_error"


_STATUS: dict[ErrorCode, int] = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.VALIDATION: 422,
    ErrorCode.CONFLICT: 409,
    ErrorCode.BLOCK_ANSWERED: 409,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.NOT_IMPLEMENTED: 501,
    ErrorCode.TOO_MANY_ATTEMPTS: 429,
    ErrorCode.INTERNAL: 500,
    ErrorCode.ACTION_CHANGED: 409,
    ErrorCode.ACTION_EXPIRED: 410,
}


class BotaiError(Exception):
    """Domain error. `detail` is shown to users: human language, never secrets."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str = "",
        *,
        status: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status or _STATUS.get(code, 400)
        self.extra = extra or {}


def problem(
    code: ErrorCode,
    detail: str,
    status: int,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": f"https://botai.mnema.app/errors/{code.value}",
        "title": code.value,
        "status": status,
        "code": code.value,
        "detail": detail,
    }
    if trace_id:
        body["traceId"] = trace_id
    if extra:
        body.update(extra)
    return body


async def botai_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, BotaiError)
    trace_id = request.headers.get("x-request-id")
    return JSONResponse(
        problem(exc.code, exc.detail, exc.status, trace_id, exc.extra),
        status_code=exc.status,
        media_type="application/problem+json",
    )


def not_implemented(what: str) -> BotaiError:
    return BotaiError(ErrorCode.NOT_IMPLEMENTED, f"{what}: ещё не реализовано")
