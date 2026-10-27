from __future__ import annotations

import httpx

from botai.main import create_app


async def test_health_and_openapi() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.get("/api/health")).json() == {"status": "ok"}
        spec = (await client.get("/api/openapi.json")).json()
    paths = spec["paths"]
    for p in [
        "/api/v1/me",
        "/api/v1/spaces",
        "/api/v1/jobs/{job_id}/events",
        "/api/v1/chat/threads/{thread_id}/context",
        "/api/v1/today",
        "/api/v1/connections/{connection_id}/login-session",
    ]:
        assert p in paths, p


def test_worker_handler_table_has_no_overlaps() -> None:
    from botai.worker import handler_table

    handler_table()
