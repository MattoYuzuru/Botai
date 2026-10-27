"""Owner: W2. Fallback remote screen: ticket + WebSocket (contracts/events.md §2).

`POST /connections/{id}/login-session` hands a signed one-time ticket (60 s) to the owner of a
connection whose login is waiting for a human. The WebSocket checks the ticket and the Origin
before the upgrade, and lets exactly one viewer watch.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket
from starlette.websockets import WebSocketDisconnect

from botai.auth.deps import AuthUser
from botai.browser.presets import LoginStore, PgLoginStore
from botai.browser.schemas import LoginSessionOut
from botai.browser.screen import (
    CLOSE_BAD_ORIGIN,
    ScreenError,
    ScreenRegistry,
    get_screens,
)
from botai.core.errors import BotaiError, ErrorCode
from botai.db.session import get_sessionmaker
from botai.settings import Settings, get_settings

router = APIRouter(prefix="/api/v1", tags=["browser"])


def get_screens_dep(settings: Annotated[Settings, Depends(get_settings)]) -> ScreenRegistry:
    """The process-wide screens. Tests override it with their own registry."""
    return get_screens(settings)


def get_login_store() -> LoginStore:
    return PgLoginStore(get_sessionmaker())


ScreensDep = Annotated[ScreenRegistry, Depends(get_screens_dep)]
LoginStoreDep = Annotated[LoginStore, Depends(get_login_store)]


@router.post(
    "/connections/{connection_id}/login-session", response_model=LoginSessionOut, status_code=201
)
async def open_login_session(
    connection_id: str, user: AuthUser, screens: ScreensDep, store: LoginStoreDep
) -> LoginSessionOut:
    """A fresh ticket for the screen this connection's login is waiting on.

    Call it right before connecting and again on every reconnect: the ticket in the `login`
    block is only a fast path, it expires after a minute."""
    await store.connection(user.id, connection_id)  # 404 for somebody else's connection
    session = screens.find(user.id, connection_id)
    if session is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Сейчас вход не ждёт вас на экране")
    offer = screens.mint_ticket(session)
    return LoginSessionOut(
        session_id=session.id, ws_url=offer.ws_url, ticket=offer.ticket, expires_at=offer.expires_at
    )


class _SocketViewer:
    def __init__(self, socket: WebSocket) -> None:
        self._socket = socket

    async def send(self, text: str) -> None:
        await self._socket.send_text(text)

    async def close(self, code: int, reason: str) -> None:
        await self._socket.close(code=code, reason=reason)


@router.websocket("/login-sessions/{session_id}/ws")
async def login_session_ws(websocket: WebSocket, session_id: str, screens: ScreensDep) -> None:
    # Everything is checked before `accept`: a refusal is an HTTP 403 on the upgrade request.
    if not screens.allowed_origin(websocket.headers.get("origin")):
        await websocket.close(code=CLOSE_BAD_ORIGIN)
        return
    try:
        session = screens.redeem(websocket.query_params.get("ticket"), session_id)
    except ScreenError as exc:
        await websocket.close(code=exc.code, reason=exc.reason)
        return
    await websocket.accept()
    viewer = _SocketViewer(websocket)
    try:
        await session.attach(viewer)
        while True:
            event = await websocket.receive()
            if event["type"] == "websocket.disconnect":
                break
            raw = event.get("text")
            await session.handle_message(
                viewer, raw if raw is not None else event.get("bytes", b"")
            )
    except (WebSocketDisconnect, ScreenError, RuntimeError):
        pass
    finally:
        await session.detach(viewer)
