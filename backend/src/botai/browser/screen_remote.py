"""The link between the API process (which holds the public WebSocket) and the worker process
(which holds the browser): in production they are separate containers.

* The worker runs `ScreenInternalServer` on a private port. It answers "which screen is open for
  this user and connection?" and attaches a viewer to a screen.
* The API uses `RemoteScreens` to ask that question and, after checking the ticket and the Origin
  itself, to relay the viewer's socket to the worker's.

Every request carries `X-Botai-Auth: <unix time>.<mac>` over its path and query, keyed from the
same secret as the tickets, so only a process that shares the deployment's secrets is served. The
port is never published outside the Docker network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Sequence
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.http11 import Request, Response

if TYPE_CHECKING:
    from botai.browser.screen import ScreenRegistry

log = logging.getLogger(__name__)
# The websockets library logs every frame at DEBUG, and here the frames are a student's keystrokes
# (their password, their one-time code). Never below WARNING, whatever the root level is.
logging.getLogger("websockets").setLevel(logging.WARNING)

AUTH_HEADER = "X-Botai-Auth"
MAX_SKEW_S = 30
FIND_PATH = "/internal/screens/find"
_WS_PATH = re.compile(r"^/internal/screens/([0-9A-Za-z]{1,40})/ws$")
_ID = re.compile(r"^[0-9A-Za-z]{1,40}$")


def _mac(key: bytes, stamp: int, target: str) -> str:
    digest = hmac.new(key, f"{stamp}|{target}".encode(), hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sign(key: bytes, target: str, *, now: float | None = None) -> str:
    stamp = int(time.time() if now is None else now)
    return f"{stamp}.{_mac(key, stamp, target)}"


def authorised(key: bytes, header: str | None, target: str, *, now: float | None = None) -> bool:
    if not header or not header.isascii():
        return False
    stamp_text, _, mac = header.partition(".")
    if not (stamp_text.isdecimal() and len(stamp_text) <= 12) or not mac:
        return False
    stamp = int(stamp_text)
    if abs((time.time() if now is None else now) - stamp) > MAX_SKEW_S:
        return False
    return hmac.compare_digest(mac, _mac(key, stamp, target))


# --------------------------------------------------------------------------------------------
# Worker side
# --------------------------------------------------------------------------------------------


class _InternalViewer:
    """The API's relay socket as a `Viewer`. Closing does not wait for the other side: the
    handler that calls it is the one reading that socket, and a peer flooding it would otherwise
    stall the closing handshake until its timeout."""

    def __init__(self, socket: ServerConnection) -> None:
        self._socket = socket
        self._closing: asyncio.Task[None] | None = None

    async def send(self, text: str) -> None:
        await self._socket.send(text)

    async def close(self, code: int, reason: str) -> None:
        if self._closing is None:
            self._closing = asyncio.get_running_loop().create_task(self._socket.close(code, reason))


class ScreenInternalServer:
    """Serves the screens of this process to the API process."""

    def __init__(self, registry: ScreenRegistry, key: bytes, host: str, port: int) -> None:
        self._registry = registry
        self._key = key
        self._bind = (host, port)
        self._server: Server | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("internal screen server is not started")
        return int(next(iter(self._server.sockets)).getsockname()[1])

    async def start(self) -> None:
        if self._server is None:
            self._server = await serve(
                self._handle,
                self._bind[0],
                self._bind[1],
                process_request=self._process,
                max_size=64 * 1024,
                ping_interval=20,
            )
            log.info("internal screen server listening on %s:%d", self._bind[0], self.port)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _process(self, connection: ServerConnection, request: Request) -> Response | None:
        if not authorised(self._key, request.headers.get(AUTH_HEADER), request.path):
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        parts = urlsplit(request.path)
        found = _WS_PATH.match(parts.path)
        if found is not None:
            user = _query(parts.query).get("user", "")
            session = self._registry.get(found.group(1))
            if session is None or session.user_id != user:
                return connection.respond(HTTPStatus.NOT_FOUND, "no such screen\n")
        elif parts.path != FIND_PATH:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        return None

    async def _handle(self, socket: ServerConnection) -> None:
        assert socket.request is not None
        parts = urlsplit(socket.request.path)
        query = _query(parts.query)
        if parts.path == FIND_PATH:
            session = self._registry.find(query.get("user", ""), query.get("connection", ""))
            await socket.send(json.dumps({"sessionId": session.id if session else None}))
            await socket.close()
            return
        found = _WS_PATH.match(parts.path)
        session = self._registry.get(found.group(1)) if found else None
        if session is None:
            await socket.close(4404, "no such screen")
            return
        viewer = _InternalViewer(socket)
        try:
            await session.attach(viewer)
            async for raw in socket:
                await session.handle_message(viewer, raw)
        except ConnectionClosed:
            pass
        finally:
            await session.detach(viewer)


def _query(query: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key:
            out[key] = value
    return out


# --------------------------------------------------------------------------------------------
# API side
# --------------------------------------------------------------------------------------------


class RemoteScreens:
    """Asks the worker(s) at `urls` for screens and opens relay sockets to them."""

    def __init__(self, urls: Sequence[str], key: bytes, *, timeout_s: float = 1.5) -> None:
        self._bases = [u.rstrip("/").replace("http://", "ws://", 1) for u in urls]
        self._key = key
        self._timeout = timeout_s

    def _url(self, base: str, path: str, **query: str) -> tuple[str, str]:
        target = path + "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in query.items())
        return base + target, target

    async def _open(self, base: str, path: str, **query: str) -> ClientConnection:
        url, target = self._url(base, path, **query)
        return await asyncio.wait_for(
            connect(
                url,
                additional_headers={AUTH_HEADER: sign(self._key, target)},
                max_size=None,
                open_timeout=self._timeout,
                ping_interval=20,
            ),
            self._timeout + 0.5,
        )

    async def find(self, user_id: str, connection_id: str) -> str | None:
        """The id of the screen open for this user and connection, wherever it lives."""
        for base in self._bases:
            try:
                socket = await self._open(base, FIND_PATH, user=user_id, connection=connection_id)
                try:
                    reply = json.loads(await asyncio.wait_for(socket.recv(), self._timeout))
                finally:
                    await socket.close()
            except (OSError, TimeoutError, InvalidHandshake, ConnectionClosed, ValueError):
                continue
            session_id = reply.get("sessionId") if isinstance(reply, dict) else None  # type: ignore[reportUnknownVariableType]
            if isinstance(session_id, str) and _ID.match(session_id):
                return session_id
        return None

    async def connect(self, session_id: str, user_id: str) -> ClientConnection | None:
        """A socket to the worker that holds the screen; None if no worker has it."""
        if not _ID.match(session_id):
            return None
        for base in self._bases:
            try:
                return await self._open(base, f"/internal/screens/{session_id}/ws", user=user_id)
            except (OSError, TimeoutError, InvalidHandshake):
                continue
        return None
