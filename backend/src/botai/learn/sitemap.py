"""The non-secret map of a source: where its sections are and which endpoints answer questions.

Observation sees the menu, the routes of the single-page app and the requests the pages make. The
map keeps what helps a later conversation find its way: section labels with paths, the routes, and
the FAQ or search endpoints (a path and the names of its query keys, never a value). It is shared
like a recipe, so it holds nothing that belongs to one student: identifiers in paths become `{id}`,
labels pass the redactor, and any address that carries a credential is left out.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.browser import ObservedRequest, PageSnapshot
from botai.db.models import SourceSitemap
from botai.learn.addresses import navigation_url
from botai.learn.redact import Redactor

MAX_NAVIGATION = 40
MAX_ROUTES = 60
MAX_ENDPOINTS = 12
_ID_SEGMENT = re.compile(r"/(?:\d{2,}|[0-9a-fA-F]{8,}|[0-9A-Z]{20,})(?=/|$)")
_FAQ = re.compile(
    r"(?i)(?:^|[/_.-])(?:faq|help|questions?|knowledge|support|вопрос\w*)(?:$|[/_.-])"
)
_SEARCH = re.compile(r"(?i)(?:^|[/_.-])(?:search|find|suggest|autocomplete|lookup)(?:$|[/_.-])")
_SEARCH_KEYS = frozenset({"q", "query", "search", "text", "term", "keyword", "s"})


def _path(url: str, origin: str) -> str | None:
    """A same-origin path with identifiers generalised, or None for anything not shareable."""
    safe = navigation_url(url)
    if safe is None:
        return None
    parts = urlsplit(safe)
    if f"{parts.scheme}://{parts.netloc}" != origin:
        return None
    path = _ID_SEGMENT.sub("/{id}", parts.path or "/")
    fragment = parts.fragment if parts.fragment.startswith("/") else ""
    route = path + (f"#{_ID_SEGMENT.sub('/{id}', fragment.partition('?')[0])}" if fragment else "")
    return route[:200]


def build_sitemap(
    origin: str,
    redactor: Redactor,
    pages: Iterable[PageSnapshot],
    links: Iterable[tuple[str, str]],
    calls: Iterable[ObservedRequest],
) -> dict[str, Any]:
    """Navigation, routes and FAQ/search endpoints seen in one observation."""
    navigation: dict[str, str] = {}
    routes: dict[str, None] = {}
    for page in pages:
        if (route := _path(page.url, origin)) is not None:
            routes.setdefault(route)
    for label, url in links:
        path = _path(url, origin)
        if path is None or redactor.contains_secret_address(url):
            continue
        routes.setdefault(path)
        text = redactor.text(label, 60)
        if text and path not in navigation:
            navigation[path] = text
    endpoints: dict[tuple[str, str], list[str]] = {}
    for call in calls:
        if call.method != "GET" or not (call.status and 200 <= call.status < 300):
            continue
        path = _path(call.url, origin)
        if path is None or redactor.contains_secret_address(call.url):
            continue
        keys = sorted(
            {key for key, _ in parse_qsl(urlsplit(call.url).query, keep_blank_values=True)}
        )
        kind = (
            "faq"
            if _FAQ.search(path)
            else "search"
            if _SEARCH.search(path) or _SEARCH_KEYS & {key.lower() for key in keys}
            else None
        )
        if kind is not None:
            endpoints[(kind, path.partition("#")[0])] = keys[:8]
    return {
        "navigation": [{"label": label, "path": path} for path, label in navigation.items()][
            :MAX_NAVIGATION
        ],
        "routes": list(routes)[:MAX_ROUTES],
        "endpoints": [
            {"kind": kind, "path": path, "queryKeys": keys}
            for (kind, path), keys in endpoints.items()
        ][:MAX_ENDPOINTS],
    }


def merge_sitemaps(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, Any]:
    """What earlier observations knew stays; the newest sections come first, within the caps."""
    old = old or {}

    def union(key: str, ident: Callable[[Any], Any], cap: int) -> list[Any]:
        merged: dict[Any, Any] = {}
        for entry in [*new.get(key, []), *old.get(key, [])]:
            merged.setdefault(ident(entry), entry)
        return list(merged.values())[:cap]

    def route(entry: Any) -> Any:
        return entry

    def navigation_key(entry: Any) -> Any:
        return entry["path"]

    def endpoint_key(entry: Any) -> Any:
        return entry["kind"], entry["path"]

    return {
        "navigation": union("navigation", navigation_key, MAX_NAVIGATION),
        "routes": union("routes", route, MAX_ROUTES),
        "endpoints": union("endpoints", endpoint_key, MAX_ENDPOINTS),
    }


async def save_site_map(
    session: AsyncSession, origin: str, observed: Mapping[str, Any], now: datetime
) -> None:
    """Merge one observation into the map of its origin (one row per origin, shared)."""
    current = await get_site_map(session, origin)
    merged = merge_sitemaps(current, observed)
    if not any(merged.values()):
        return
    statement = insert(SourceSitemap).values(origin=origin, map=merged, updated_at=now)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[SourceSitemap.origin],
            set_={"map": statement.excluded.map, "updated_at": statement.excluded.updated_at},
        )
    )


async def get_site_map(session: AsyncSession, origin: str) -> dict[str, Any] | None:
    """The saved map of an origin: `{navigation: [{label, path}], routes: [path], endpoints:
    [{kind: faq|search, path, queryKeys}]}`, or None when nothing was observed yet."""
    row = await session.scalar(select(SourceSitemap.map).where(SourceSitemap.origin == origin))
    return dict(row) if row is not None else None
