"""FastAPI app. Routers are owned by modules; this file only wires them (owner: W0)."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from botai.agent.router import router as agent_router
from botai.attachments.limits import UploadSizeLimit
from botai.attachments.router import router as attachments_router
from botai.auth.router import router as auth_router
from botai.browser.router import router as browser_router
from botai.core.diagnostics import diagnostic_event, diagnostic_scope, url_facts
from botai.core.errors import BotaiError, ErrorCode, botai_error_handler, problem
from botai.core.ids import new_id
from botai.core.logging import configure_logging
from botai.domain.router import router as domain_router
from botai.jobs.router import router as jobs_router
from botai.settings import get_settings
from botai.sources.router import router as sources_router
from botai.spaces.router import router as spaces_router
from botai.speech.router import get_speech
from botai.speech.router import router as speech_router
from botai.usage.router import router as usage_router


def _mount_spa(app: FastAPI, static_dir: Path) -> None:
    """Serve the built PWA in production. API paths never fall through to index.html."""
    root = static_dir.resolve()
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(root / "index.html", headers={"cache-control": "no-cache"})


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="BotAI API", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json"
    )
    app.add_exception_handler(BotaiError, botai_error_handler)

    @app.middleware("http")
    async def diagnostic_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not get_settings().botai_diagnostics or request.url.path == "/api/health":
            return await call_next(request)
        request_id = new_id()
        started = time.monotonic()
        with diagnostic_scope(request_id=request_id):
            try:
                response = await call_next(request)
            except Exception:
                diagnostic_event(
                    "http.failed",
                    method=request.method,
                    status=500,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error_code="internal_error",
                    **url_facts(str(request.url)),
                )
                raise
            diagnostic_event(
                "http.complete",
                method=request.method,
                status=response.status_code,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                **url_facts(str(request.url)),
                **{
                    k: v
                    for k, v in request.path_params.items()
                    if k in {"thread_id", "job_id", "connection_id"}
                },
            )
        response.headers["x-request-id"] = request_id
        return response

    app.add_middleware(UploadSizeLimit)  # outermost: refuses an oversized upload unread

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        # Pydantic errors contain raw input, which can be a password or signed URL.
        return JSONResponse(
            problem(ErrorCode.VALIDATION, "Проверьте формат запроса", 422),
            status_code=422,
            media_type="application/problem+json",
        )

    for r in (
        auth_router,
        spaces_router,
        domain_router,
        jobs_router,
        usage_router,
        browser_router,
        sources_router,
        attachments_router,
        speech_router,
        agent_router,
    ):
        app.include_router(r)

    get_speech()  # builds the dictation service now: wrong STT_* settings are logged at start

    @app.get("/api/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    static_dir = get_settings().static_dir
    if static_dir and (Path(static_dir) / "index.html").is_file():
        _mount_spa(app, Path(static_dir))
    return app


app = create_app()
