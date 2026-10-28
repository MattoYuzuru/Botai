"""Owner: W2. Fallback remote screen: ticket + WebSocket (contracts/events.md §2).

`POST /connections/{id}/login-session` hands a signed one-time ticket (60 s) to the owner of a
connection whose login is waiting for a human. The WebSocket checks the ticket and the Origin
before the upgrade, and lets exactly one viewer watch.

The screen lives next to the browser, in the worker process. When this API process does not hold
it, the socket is relayed to the worker (`screen_remote.py`): the checks stay here.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from botai.auth.deps import AuthUser
from botai.browser.presets import LoginStore, PgLoginStore
from botai.browser.schemas import LoginSessionOut
from botai.browser.screen import (
    CLOSE_BAD_ORIGIN,
    CLOSE_NO_SESSION,
    ScreenError,
    ScreenRegistry,
    ScreenSession,
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
    session_id = await screens.lookup(user.id, connection_id, request_native=True)
    if session_id is None:
        raise BotaiError(ErrorCode.NOT_FOUND, "Сейчас вход не ждёт вас на экране")
    offer = screens.mint_ticket_for(session_id, user.id)
    return LoginSessionOut(
        session_id=session_id, ws_url=offer.ws_url, ticket=offer.ticket, expires_at=offer.expires_at
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
        claims = screens.spend(websocket.query_params.get("ticket"), session_id)
        local = screens.get(session_id)
        if local is not None:
            if local.user_id != claims.user_id:
                raise ScreenError(CLOSE_NO_SESSION, "no such session")
            await websocket.accept()
            await _serve_here(websocket, local)
            return
        upstream = await screens.connect_remote(session_id, claims.user_id)
    except ScreenError as exc:
        await websocket.close(code=exc.code, reason=exc.reason)
        return
    await websocket.accept()
    await _relay(websocket, upstream)


async def _serve_here(websocket: WebSocket, session: ScreenSession) -> None:
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


def _sendable(code: int) -> int:
    """A close code that may go on the wire (1005/1006 are only ever local)."""
    return code if code in (1000, 1001, 1008, 1011) or 3000 <= code <= 4999 else 1011


async def _relay(public: WebSocket, upstream: ClientConnection) -> None:
    """Pipe the viewer's socket to the worker's, both ways, until either side is done. The
    worker's close code (replaced, expired, success) reaches the viewer as it is."""

    async def to_worker() -> None:
        while True:
            event = await public.receive()
            if event["type"] == "websocket.disconnect":
                return
            data = event.get("text")
            await upstream.send(data if data is not None else event.get("bytes", b""))

    async def to_viewer() -> None:
        async for message in upstream:
            if isinstance(message, str):
                await public.send_text(message)
            else:
                await public.send_bytes(message)

    tasks = [asyncio.create_task(to_worker()), asyncio.create_task(to_viewer())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if upstream.close_code is not None:  # the worker ended it: tell the viewer why
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await public.close(code=_sendable(upstream.close_code), reason=upstream.close_reason)
    with contextlib.suppress(ConnectionClosed, RuntimeError):
        await upstream.close()
