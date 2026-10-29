"""Turns a fetched page into text for the model: HTML -> text and links, JSON -> a short outline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from botai.sources.extract import collapse, node_text

TEXT_CHARS = 12_000
JSON_CHARS = 12_000
MAX_LINKS = 40
LABEL_CHARS = 80
_JSON_LIST_ITEMS = 8
_JSON_STRING_CHARS = 300
_JSON_DEPTH = 6


@dataclass(slots=True)
class Page:
    title: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (label, https url)


def normalize_url(url: str) -> str:
    """A link as it is compared: no fragment, no trailing slash."""
    return urldefrag(url.strip())[0].rstrip("/")


def page_links(data: bytes, base_url: str, *, limit: int = MAX_LINKS) -> list[tuple[str, str]]:
    tree = LexborHTMLParser(data)
    seen: dict[str, str] = {}
    for anchor in tree.css("a[href]"):
        href = (anchor.attributes.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        target = urljoin(base_url, href)
        parts = urlsplit(target)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            continue
        key = normalize_url(target)
        if key not in seen:
            seen[key] = collapse(node_text(anchor))[:LABEL_CHARS] or parts.path or "ссылка"
        if len(seen) >= limit:
            break
    return [(label, url) for url, label in seen.items()]


def shrink_json(value: Any, depth: int = 0) -> Any:
    """Keep the shape of a JSON answer and a few items of every list: enough to see what is there."""
    if depth >= _JSON_DEPTH:
        return "…"
    if isinstance(value, dict):
        return {str(k): shrink_json(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        head: list[Any] = [shrink_json(v, depth + 1) for v in value[:_JSON_LIST_ITEMS]]
        if len(value) > _JSON_LIST_ITEMS:
            head.append(f"…и ещё {len(value) - _JSON_LIST_ITEMS} элементов (всего {len(value)})")
        return head
    if isinstance(value, str) and len(value) > _JSON_STRING_CHARS:
        return value[:_JSON_STRING_CHARS] + "…"
    return value


def json_text(raw: bytes) -> str | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    text = json.dumps(shrink_json(value), ensure_ascii=False, separators=(",", ":"))
    if len(text) > JSON_CHARS:
        text = text[:JSON_CHARS] + "…[обрезано]"
    return text
