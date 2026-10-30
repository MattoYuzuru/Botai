"""A cap on the body of an upload, before anything of it is spooled to disk.

Starlette reads a whole multipart body (into memory up to a megabyte, then into a temporary
file) before a route can look at the file, so the limit of a route that reads `limit + 1` bytes
protects nothing: a client can send gigabytes. This middleware answers 413 from the declared
`Content-Length` at once, and counts the bytes of a body that declares none (chunked) and cuts
it off when it passes the limit. It looks only at the upload routes; a dictation has a limit of
its own (a recording is a few megabytes, not twenty).
"""

from __future__ import annotations

import json
import re
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from botai.core.errors import ErrorCode, problem
from botai.sources.documents import MAX_UPLOAD
from botai.speech import limits as speech_limits

# The file plus the multipart framing and the other fields.
BODY_LIMIT = MAX_UPLOAD + 1024 * 1024
UPLOAD_ROUTES = re.compile(r"^/api/v1/(?:attachments|spaces/[^/]+/documents/upload)$")
_MESSAGE = "Файл больше 20 МБ"
_SPEECH_MESSAGE = "Запись больше 5 МБ"


class UploadSizeLimit:
    def __init__(
        self, app: ASGIApp, *, limit: int = BODY_LIMIT, speech_limit: int = speech_limits.BODY_LIMIT
    ) -> None:
        self.app, self.limit, self.speech_limit = app, limit, speech_limit

    def _rule(self, scope: Scope) -> tuple[int, str] | None:
        if scope["type"] != "http" or scope["method"] != "POST":
            return None
        if scope["path"] == speech_limits.ROUTE:
            return self.speech_limit, _SPEECH_MESSAGE
        if UPLOAD_ROUTES.match(scope["path"]):
            return self.limit, _MESSAGE
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        rule = self._rule(scope)
        if rule is None:
            await self.app(scope, receive, send)
            return
        limit, message = rule
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._refuse(send, message)
            return

        seen = 0
        over = False

        async def counted() -> Message:
            nonlocal seen, over
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
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
            await self._refuse(send, message)

    async def _refuse(self, send: Send, message: str) -> None:
        body = _problem_json(message)
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/problem+json"),
            (b"content-length", str(len(body)).encode()),
            (b"connection", b"close"),  # the rest of the body is not read
        ]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _problem_json(message: str) -> bytes:
    payload: dict[str, Any] = problem(ErrorCode.VALIDATION, message, 413)
    return json.dumps(payload, ensure_ascii=False).encode()
