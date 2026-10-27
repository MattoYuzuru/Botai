"""Owner: W2. Fallback remote screen: ticket + WebSocket (contracts/events.md §2)."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from botai.browser.schemas import LoginSessionOut
from botai.core.errors import not_implemented

router = APIRouter(prefix="/api/v1", tags=["browser"])


@router.post(
    "/connections/{connection_id}/login-session", response_model=LoginSessionOut, status_code=201
)
async def open_login_session(connection_id: str) -> LoginSessionOut:
    raise not_implemented("open_login_session")


@router.websocket("/login-sessions/{session_id}/ws")
async def login_session_ws(websocket: WebSocket, session_id: str) -> None:
    await websocket.close(code=1011, reason="not implemented")
