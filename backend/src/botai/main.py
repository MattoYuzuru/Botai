"""FastAPI app. Routers are owned by modules; this file only wires them (owner: W0)."""

from __future__ import annotations

from fastapi import FastAPI

from botai.agent.router import router as agent_router
from botai.auth.router import router as auth_router
from botai.browser.router import router as browser_router
from botai.core.errors import BotaiError, botai_error_handler
from botai.core.logging import configure_logging
from botai.domain.router import router as domain_router
from botai.jobs.router import router as jobs_router
from botai.sources.router import router as sources_router
from botai.spaces.router import router as spaces_router
from botai.usage.router import router as usage_router


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

    return app


app = create_app()
