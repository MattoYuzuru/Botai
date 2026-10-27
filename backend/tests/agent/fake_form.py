"""A form endpoint of the synthetic university for the action skeleton (fakeuni host)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from botai.agent.actions import ExecutionResult
from botai.core.http import build_client
from botai.core.jobs import JobContext


def create_form_app(received: list[dict[str, str]]) -> FastAPI:
    app = FastAPI()

    @app.post("/forms/transcript")
    async def transcript(request: Request) -> Any:
        form = await request.form()
        received.append({k: str(v) for k, v in form.items()})
        return {"ok": True, "number": "T-0001"}

    @app.post("/forms/broken")
    async def broken(request: Request) -> Any:
        received.append({})
        return JSONResponse({"error": "internal"}, status_code=500)

    return app


class FakeuniFormExecutor:
    """Posts through `core.http.build_client`, so the connection's host allowlist still applies."""

    def __init__(self, received: list[dict[str, str]]) -> None:
        self.received = received
        self.transport = httpx.ASGITransport(app=create_form_app(received))

    async def execute(
        self,
        ctx: JobContext,
        *,
        allow_hosts: Sequence[str],
        method: str,
        url: str,
        body_kind: str,
        fields: Mapping[str, str],
    ) -> ExecutionResult:
        async with build_client(
            allow_hosts=allow_hosts, proxy_url=None, transport=self.transport
        ) as client:
            response = await client.request(method, url, data=dict(fields))
        ok = response.is_success
        return ExecutionResult(
            ok, response.status_code, "Заявка отправлена." if ok else "Сайт вуза не принял заявку."
        )
