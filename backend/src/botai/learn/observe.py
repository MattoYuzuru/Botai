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
from urllib.parse import urldefrag, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from botai.core.browser import BrowserHandle, ObservedRequest, PageSnapshot
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
    observed: set[str] = field(default_factory=set)
    injections: int = 0
    plain_text: str = ""  # redacted visible text of the pages, for light keyword checks

    @property
    def empty(self) -> bool:
        return not self.pages and not self.calls

    def render(self, limit: int = 30_000) -> str:
        """The bundle as the model sees it, wrapped so that it reads as data."""
        parts = [f'<observation origin="{self.origin}">']
        for page in self.pages:
            links = "; ".join(f"{text} -> {path}" for text, path in page.links)
            parts.append(f"[page] {page.url} | title: {page.title}")
            if links:
                parts.append(f"  links: {links}")
            parts.append(f"  html: {page.html}")
        for call in self.calls:
            parts.append(f"[request] {call.method} {call.url} -> {call.status} {call.content_type}")
            if call.body:
                parts.append(f"  {call.body}")
        parts.append("</observation>")
        text = "\n".join(parts)
        return text if len(text) <= limit else text[:limit] + "\n…</observation>"


# --- links ----------------------------------------------------------------------------------


def menu_links(html: str, base_url: str, origin: str) -> list[tuple[str, str]]:
    """Same-origin links worth visiting: the menu first, then the rest, without destructive ones."""
    tree = LexborHTMLParser(html)
    ordered: list[LexborNode] = list(tree.css(_MENU)) + list(tree.css("a[href]"))
    seen: set[str] = {urlsplit(base_url).path or "/"}
    out: list[tuple[str, str]] = []
    for node in ordered:
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0]
        parts = urlsplit(absolute)
        if f"{parts.scheme}://{parts.netloc}" != origin or _STATIC.search(parts.path):
            continue
        label = node_text(node)
        if _SKIP_LINK.search(absolute) or _SKIP_LINK.search(label):
            continue
        key = parts.path or "/"
        if key in seen:
            continue
        seen.add(key)
        out.append((label[:60], absolute))
        if len(out) >= MAX_LINKS_PER_PAGE:
            break
    return out


# --- the observer ---------------------------------------------------------------------------


async def _visit(handle: BrowserHandle, url: str) -> PageSnapshot | None:
    try:
        return await handle.goto(url)
    except Exception as exc:
        log.info("observation skipped a page: %s", type(exc).__name__)
        return None


def _body_view(redactor: Redactor, sample: str | None) -> str:
    if not sample:
        return ""
    text = sample.strip()
    if text[:1] in "{[":
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if doc is not None:
            return json.dumps(redactor.json(doc), ensure_ascii=False, separators=(",", ":"))[
                :MAX_BODY_CHARS
            ]
    if "<" in text[:200] and ">" in text[:400]:
        return redactor.html(text, 1500)
    return (redactor.text(text, 600) or "")[:600]


def _interesting(call: ObservedRequest, origin: str) -> bool:
    parts = urlsplit(call.url)
    if f"{parts.scheme}://{parts.netloc}" != origin or _STATIC.search(parts.path):
        return False
    ctype = (call.response_content_type or "").lower()
    return not any(skip in ctype for skip in _SKIP_TYPES)


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
    seen_calls: set[tuple[str, str]] = set()
    kept: list[ObservedRequest] = []
    for call in log_entries:
        if not _interesting(call, origin):
            continue
        key = (call.method, urlsplit(call.url).path)
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
    except ValueError:
        return []
