"""Owner: W3. HTML-only pages (for HTML recipes)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/news", response_class=HTMLResponse)
async def news() -> str:
    return """<!doctype html><html><body><main><ul class="news-list">
<li class="news-item"><a href="/news/1">Перенос занятий 4 ноября</a><time datetime="2026-10-27">27 октября</time></li>
<li class="news-item"><a href="/news/2">Запись на пересдачу открыта</a><time datetime="2026-10-25">25 октября</time></li>
</ul></main></body></html>"""
