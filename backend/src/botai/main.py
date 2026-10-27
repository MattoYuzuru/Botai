"""FastAPI app. Routers are owned by modules; this file only wires them (owner: W0)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from botai.agent.router import router as agent_router
from botai.auth.router import router as auth_router
from botai.browser.router import router as browser_router
from botai.core.errors import BotaiError, botai_error_handler
from botai.core.logging import configure_logging
from botai.domain.router import router as domain_router
from botai.jobs.router import router as jobs_router
from botai.settings import get_settings
from botai.sources.router import router as sources_router
from botai.spaces.router import router as spaces_router
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
    for r in (
        auth_router,
        spaces_router,
        domain_router,
        jobs_router,
        usage_router,
        browser_router,
        sources_router,
        agent_router,
    ):
        app.include_router(r)

    @app.get("/api/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    static_dir = get_settings().static_dir
    if static_dir and (Path(static_dir) / "index.html").is_file():
        _mount_spa(app, Path(static_dir))
    return app


app = create_app()
