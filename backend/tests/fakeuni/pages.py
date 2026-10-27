"""Owner: W3. HTML-only pages (for HTML recipes)."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from tests.fakeuni import data
from tests.fakeuni.login import is_logged_in

router = APIRouter()

NEWS = [
    ("/news/1", "Перенос занятий 4 ноября", "2026-10-27", "27 октября"),
    ("/news/2", "Запись на пересдачу открыта", "2026-10-25", "25 октября"),
]


def _state() -> object:
    from tests.fakeuni import api

    return api.STATE


@router.get("/news", response_class=HTMLResponse)
async def news() -> str:
    markup = getattr(_state(), "news_markup", "v1")
    if markup == "v2":
        items = "\n".join(
            f'<article class="post"><h3><a href="{href}">{escape(title)}</a></h3>'
            f'<span class="when" data-iso="{iso}">{human}</span></article>'
            for href, title, iso, human in NEWS
        )
        return f"<!doctype html><html><body><main><section>{items}</section></main></body></html>"
    items = "\n".join(
        f'<li class="news-item"><a href="{href}">{escape(title)}</a>'
        f'<time datetime="{iso}">{human}</time></li>'
        for href, title, iso, human in NEWS
    )
    return (
        '<!doctype html><html><body><main><ul class="news-list">\n'
        f"{items}\n</ul></main></body></html>"
    )


@router.get("/portal/deadlines", response_model=None)
async def deadlines_page(request: Request) -> Response:
    """Deadlines as an HTML table behind the login (a session that died redirects to sign-in)."""
    if not is_logged_in(request):
        return HTMLResponse(status_code=302, headers={"location": "/auth/login"})
    rows = "\n".join(
        f'<tr data-task="{d["taskId"]}"><td class="name">{escape(str(d["name"]))}</td>'
        f'<td class="course">{escape(str(d["course"]))}</td>'
        f'<td class="due" data-ts="{d["due"]}">'
        f"{datetime.fromtimestamp(int(str(d['due'])), tz=UTC):%d.%m.%Y %H:%M}</td></tr>"
        for d in data.deadlines()
    )
    return HTMLResponse(
        "<!doctype html><html><head><title>Задания</title></head><body>"
        f'<table id="tasks"><thead><tr><th>Задание</th><th>Курс</th><th>Срок</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></body></html>"
    )
