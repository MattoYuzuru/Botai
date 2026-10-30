"""Turns a fetched page into text for the model: HTML -> text and links, JSON -> a short outline."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urldefrag, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from botai.agent.safety import HIDDEN, scrub_secrets
from botai.learn.redact import JWT, LONG_TOKEN, secret_query_key
from botai.sources.extract import collapse, node_text

TEXT_CHARS = 12_000
JSON_CHARS = 12_000
MAX_LINKS = 40
LABEL_CHARS = 80
_JSON_LIST_ITEMS = 8
_JSON_STRING_CHARS = 300
_JSON_DEPTH = 6
# A key that names a credential: its value is hidden whatever it looks like.
_SECRET_KEY = re.compile(
    r"(?i:token|session|sesskey|sessid|(?:^|[_-])sid$|csrf|xsrf|secret|passw|pwd|auth(?!ors?$)|"
    r"api[_-]?key|private[_-]?key|credential|cookie|bearer|jwt|signature|(?:^|[_-])otp$|"
    r"^pin$|(?:^|[_-])key$)|[a-z](?:Key|Sid)$"
)


def has_login_form(data: bytes) -> bool:
    """A page that asks for a password: where an expired session ends up on sites without a 401."""
    return LexborHTMLParser(data).css_first("input[type=password]") is not None


def has_secret_address(url: str) -> bool:
    parts = urlsplit(url)
    values = [*parse_qsl(parts.query, keep_blank_values=True), *parse_qsl(parts.fragment)]
    return (
        any(
            secret_query_key(k) or _SECRET_KEY.search(k) or JWT.search(v) or LONG_TOKEN.search(v)
            for k, v in values
        )
        or JWT.search(parts.path) is not None
        or LONG_TOKEN.search(parts.path) is not None
    )


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
        if has_secret_address(target):
            continue  # a link that carries a session key is neither shown nor opened
        key = normalize_url(target)
        if key not in seen:
            seen[key] = collapse(node_text(anchor))[:LABEL_CHARS] or parts.path or "ссылка"
        if len(seen) >= limit:
            break
    return [(label, url) for url, label in seen.items()]


def mask_text(text: str) -> str:
    """Credential-shaped strings out of page text: tokens, JWTs, `token=…`, cookie lines."""
    return LONG_TOKEN.sub(HIDDEN, JWT.sub(HIDDEN, scrub_secrets(text)))


def shrink_json(value: Any, depth: int = 0, key: str = "") -> Any:
    """Keep the shape of a JSON answer and a few items of every list: enough to see what is there.
    The value of a key that names a credential (`access_token`, `sesskey`) and any string that
    looks like a token are hidden: the answer of a source is data for the model, not a keyring."""
    if depth >= _JSON_DEPTH:
        return "…"
    if key and _SECRET_KEY.search(key) and not isinstance(value, (dict, list)):
        return HIDDEN
    if isinstance(value, dict):
        return {str(k): shrink_json(v, depth + 1, str(k)) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        head: list[Any] = [shrink_json(v, depth + 1, key) for v in value[:_JSON_LIST_ITEMS]]
        if len(value) > _JSON_LIST_ITEMS:
            head.append(f"…и ещё {len(value) - _JSON_LIST_ITEMS} элементов (всего {len(value)})")
        return head
    if isinstance(value, str):
        masked = mask_text(value[:_JSON_STRING_CHARS])
        return masked + ("…" if len(value) > _JSON_STRING_CHARS else "")
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
