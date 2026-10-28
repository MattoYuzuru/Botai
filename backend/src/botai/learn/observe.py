"""Deterministic observation of a university site for the Explorer (no model involved).

Starting from the home page, the observer follows a bounded number of same-origin menu links with
the user's session, collects page snapshots and the network log, and turns them into a redacted
bundle for the prompt. The model never drives the browser: it only reads this bundle.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urldefrag, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from botai.core.browser import BrowserHandle, ObservedRequest, PageSnapshot
from botai.learn.addresses import navigation_url
from botai.learn.redact import Redactor, collect_values
from botai.sources.extract import node_text

log = logging.getLogger(__name__)

MAX_PAGES = 6
MAX_CALLS = 30
MAX_LINKS_PER_PAGE = 12
MAX_BODY_CHARS = 2500
_STATIC = re.compile(
    r"(?i)\.(?:css|js|mjs|map|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|webp|pdf|zip)(?:\?|$)"
)
_SKIP_LINK = re.compile(
    r"(?i)logout|log-out|signout|sign-out|/exit|delete|remove|unsubscribe|выйти|выход|удалить|"
    r"отписаться|password|парол"
)
_MENU = "nav a[href], header a[href], aside a[href], [role=navigation] a[href], .menu a[href], .sidebar a[href]"
_SKIP_TYPES = ("image/", "font/", "text/css", "javascript", "video/", "audio/")


@dataclass(slots=True)
class PageView:
    url: str
    title: str
    links: list[tuple[str, str]]
    html: str


@dataclass(slots=True)
class CallView:
    method: str
    url: str
    status: int | None
    content_type: str
    body: str


@dataclass(slots=True)
class Observations:
    origin: str
    pages: list[PageView] = field(default_factory=list)
    calls: list[CallView] = field(default_factory=list)
    observed: set[str] = field(default_factory=set, repr=False)
    injections: int = 0
    plain_text: str = ""  # redacted visible text of the pages, for light keyword checks

    @property
    def empty(self) -> bool:
        return not self.pages and not self.calls

    def render(self, limit: int = 30_000) -> str:
        """The bundle as the model sees it, wrapped so that it reads as data."""
        parts = [f'<observation origin="{self.origin}">']
        headers: list[str] = []
        for page in self.pages:
            links = "; ".join(f"{text} -> {path}" for text, path in page.links)
            headers.append(
                f"[page] {page.url[:600]} | title: {page.title}\n"
                + (f"  links: {links[:400]}\n" if links else "")
            )
        for call in self.calls:
            headers.append(
                f"[request] {call.method} {call.url[:600]} -> {call.status} {call.content_type}\n"
            )
        # Every entry has a share after header costs. A large SPA cannot hide all later XHRs,
        # and the bundle never cuts a JSON document halfway through a field.
        entries = max(1, len(self.pages) + len(self.calls))
        share = max(
            0, (limit - len(parts[0]) - sum(map(len, headers)) - entries * 12 - 30) // entries
        )
        for i, page in enumerate(self.pages):
            parts.append(headers[i].rstrip())
            html = page.html if len(page.html) <= share else Redactor().html(page.html, share)
            parts.append(f"  html: {html}")
        for i, call in enumerate(self.calls):
            parts.append(headers[len(self.pages) + i].rstrip())
            if call.body and share >= 40:
                body = call.body
                if len(body) > share:
                    body = _body_view(Redactor(), body, share)
                parts.append(f"  {body}")
        parts.append("</observation>")
        text = "\n".join(parts)
        # Very small caller budgets may not fit even the headers. Do not expose broken data.
        return (
            text
            if len(text) <= limit
            else parts[0] + "\n[observation budget exceeded]\n</observation>"
        )


# --- links ----------------------------------------------------------------------------------


def menu_links(html: str, base_url: str, origin: str) -> list[tuple[str, str]]:
    """Same-origin links worth visiting: the menu first, then the rest, without destructive ones."""
    tree = LexborHTMLParser(html)
    ordered: list[LexborNode] = list(tree.css(_MENU)) + list(tree.css("a[href]"))
    seen = {_url_key(base_url)}
    out: list[tuple[str, str]] = []
    for node in ordered:
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0]
        parts = urlsplit(absolute)
        if f"{parts.scheme}://{parts.netloc}" != origin or _STATIC.search(parts.path):
            continue
        if navigation_url(absolute) is None:
            continue
        label = node_text(node)
        if _SKIP_LINK.search(absolute) or _SKIP_LINK.search(label):
            continue
        key = _url_key(absolute)
        if key in seen:
            continue
        seen.add(key)
        out.append((label[:60], absolute))
        if len(out) >= MAX_LINKS_PER_PAGE:
            break
    return out


def _url_key(url: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    parts = urlsplit(url)
    # Different selected weeks/pages are distinct; ordering of the same query is not.
    return parts.path or "/", tuple(sorted(parse_qsl(parts.query, keep_blank_values=True)))


# --- the observer ---------------------------------------------------------------------------


async def _visit(handle: BrowserHandle, url: str) -> PageSnapshot | None:
    try:
        return await handle.goto(url)
    except Exception as exc:
        log.info("observation skipped a page: %s", type(exc).__name__)
        return None


def _body_view(redactor: Redactor, sample: str | None, limit: int = MAX_BODY_CHARS) -> str:
    if not sample:
        return ""
    text = sample.strip()
    if text[:1] in "{[":
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            doc = None
        if doc is not None:
            return redactor.json_text(doc, limit)
    if "<" in text[:200] and ">" in text[:400]:
        return redactor.html(text, min(1500, limit))
    return redactor.text(text, min(600, limit)) or ""


def _interesting(call: ObservedRequest, origin: str) -> bool:
    parts = urlsplit(call.url)
    if f"{parts.scheme}://{parts.netloc}" != origin or _STATIC.search(parts.path):
        return False
    ctype = (call.response_content_type or "").lower()
    return not any(skip in ctype for skip in _SKIP_TYPES)


def _call_priority(call: ObservedRequest) -> int:
    """Useful successful record responses outrank envelopes and repeated background noise."""
    score = int(call.resource_type in {"xhr", "fetch"})
    if call.status is None or not 200 <= call.status < 300:
        return score - 3
    sample = (call.response_sample or "").strip()
    if sample[:1] not in "{[":
        return score
    try:
        doc = json.loads(sample)
    except (ValueError, RecursionError):
        return score

    def has_records(value: Any, depth: int = 0) -> bool:
        if depth > 8:
            return False
        if isinstance(value, list):
            return any(isinstance(item, dict) and bool(item) for item in value[:3])
        if isinstance(value, dict):
            return any(has_records(item, depth + 1) for item in list(value.values())[:40])
        return False

    return score + (4 if has_records(doc) else 0)


async def observe(
    handle: BrowserHandle,
    origin: str,
    redactor: Redactor,
    *,
    start_urls: list[str],
    max_pages: int = MAX_PAGES,
) -> Observations:
    """Visit the start pages and the menu links they lead to, then read the network log."""
    snapshots: list[tuple[str, PageSnapshot]] = []
    visited: set[str] = set()
    queue = list(start_urls)
    while queue and len(snapshots) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        snap = await _visit(handle, url)
        if snap is None:
            continue
        snapshots.append((url, snap))
        if len(snapshots) == 1 or len(queue) < max_pages:
            for _, link in menu_links(snap.html, snap.url or url, origin):
                if link not in visited and link not in queue:
                    queue.append(link)
    try:
        log_entries = await handle.network_log()
    except Exception:
        log_entries = []

    obs = Observations(origin=origin)
    # URLs first: that is where identifiers are met for the first time and get their pseudonyms
    for _, snap in snapshots:
        redactor.url(snap.url, origin)
    seen_calls: set[tuple[str, tuple[str, tuple[tuple[str, str], ...]]]] = set()
    kept: list[ObservedRequest] = []
    for call in sorted(log_entries, key=_call_priority, reverse=True):
        if not _interesting(call, origin):
            continue
        key = (call.method, _url_key(call.url))
        if key in seen_calls:
            continue
        seen_calls.add(key)
        redactor.url(call.url, origin)
        redactor.observed.update(raw_json_values(call.response_sample))
        kept.append(call)
        if len(kept) >= MAX_CALLS:
            break

    texts: list[str] = []
    for _, snap in snapshots:
        links = [
            (redactor.text(label, 60) or "", redactor.url(link, origin))
            for label, link in menu_links(snap.html, snap.url, origin)
        ]
        title = redactor.text(snap.title, 100) or ""
        obs.pages.append(
            PageView(
                url=redactor.url(snap.url, origin),
                title=title,
                links=links,
                html=redactor.html(snap.html),
            )
        )
        texts.append(redactor.text(snap.text or _plain(snap.html), 1500) or "")
    for call in kept:
        obs.calls.append(
            CallView(
                method=call.method,
                url=redactor.url(call.url, origin),
                status=call.status,
                content_type=(call.response_content_type or "").split(";")[0],
                body=_body_view(redactor, call.response_sample),
            )
        )
    obs.observed = set(redactor.observed)
    obs.injections = redactor.injections
    obs.plain_text = "\n".join(t for t in texts if t)
    return obs


def _plain(html: str) -> str:
    tree = LexborHTMLParser(html)
    for bad in tree.css("script,style"):
        bad.decompose()
    body = tree.body
    return body.text(separator=" ", strip=True) if body is not None else ""


def raw_json_values(sample: str | None) -> list[str]:
    """Scalars of a response sample, for the lint's observed set (kept apart from the prompt)."""
    if not sample or sample.strip()[:1] not in "{[":
        return []
    try:
        return list(collect_values(json.loads(sample)))
    except (ValueError, RecursionError):
        return []
