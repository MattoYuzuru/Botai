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
    # Our own guard stopped a request of the login: the site is not down.
    LOGIN_GUARD_REFUSED = "login_guard_refused"
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
    # dictation: the speech-to-text service is off, down or asked us to slow down
    STT_UNAVAILABLE = "stt_unavailable"
    STT_RATE_LIMITED = "stt_rate_limited"
    # generic API
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    UNAUTHORIZED = "unauthorized"
    VALIDATION = "validation"
    CONFLICT = "conflict"
    BLOCK_ANSWERED = "block_answered"
    # 409 on sending a message: the client decides what to do from the code
    AGENT_BUSY = "agent_busy"  # the agent's turn is still running: retry later
    SECRET_IN_CHAT = "secret_in_chat"  # noqa: S105 - a code name; looks like a secret in chat: delete
    REQUEST_REUSED = "request_reused"  # the client request id was used for another body
    STALE_TARGET = "stale_target"  # the addressed task has ended or was replaced
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
    ErrorCode.AGENT_BUSY: 409,
    ErrorCode.SECRET_IN_CHAT: 409,
    ErrorCode.REQUEST_REUSED: 409,
    ErrorCode.STALE_TARGET: 409,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.STT_UNAVAILABLE: 503,
    ErrorCode.STT_RATE_LIMITED: 429,
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
