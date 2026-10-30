"""A cap on the body of an upload, before anything of it is spooled to disk.

Starlette reads a whole multipart body (into memory up to a megabyte, then into a temporary
file) before a route can look at the file, so the limit of a route that reads `limit + 1` bytes
protects nothing: a client can send gigabytes. This middleware answers 413 from the declared
`Content-Length` at once, and counts the bytes of a body that declares none (chunked) and cuts
it off when it passes the limit. It looks only at the upload routes.
"""

from __future__ import annotations

import json
import re
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from botai.core.errors import ErrorCode, problem
from botai.sources.documents import MAX_UPLOAD

# The file plus the multipart framing and the other fields.
BODY_LIMIT = MAX_UPLOAD + 1024 * 1024
UPLOAD_ROUTES = re.compile(
    r"^/api/v1/(?:attachments|speech/transcribe|spaces/[^/]+/documents/upload)$"
)
_MESSAGE = "Файл больше 20 МБ"


class UploadSizeLimit:
    def __init__(self, app: ASGIApp, *, limit: int = BODY_LIMIT) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not UPLOAD_ROUTES.match(scope["path"])
        ):
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.limit:
            await self._refuse(send)
            return

        seen = 0
        over = False

        async def counted() -> Message:
            nonlocal seen, over
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    over = True
                    return {"type": "http.disconnect"}  # the route stops reading
            return message

        async def guarded(message: Message) -> None:
            if not over:  # whatever the cut-off route says is replaced by the 413 below
                await send(message)

        try:
            await self.app(scope, counted, guarded)
        except Exception:
            if not over:
                raise
        if over:
            await self._refuse(send)

    async def _refuse(self, send: Send) -> None:
        body = _problem_json()
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/problem+json"),
            (b"content-length", str(len(body)).encode()),
            (b"connection", b"close"),  # the rest of the body is not read
        ]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _problem_json() -> bytes:
    payload: dict[str, Any] = problem(ErrorCode.VALIDATION, _MESSAGE, 413)
    return json.dumps(payload, ensure_ascii=False).encode()
