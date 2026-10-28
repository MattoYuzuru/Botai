"""User-supplied navigation addresses, kept distinct from a recipe's single origin.

An entry query can select a week or a view. It must survive observation unchanged; it is not a
license to save a signed URL or send an authentication callback to the model.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from botai.browser.proxy import is_ip_literal, normalize_host
from botai.learn.redact import JWT, LONG_TOKEN, secret_query_key

MAX_ADDRESS = 1000


def navigation_url(value: object) -> str | None:
    """A bounded HTTPS DNS URL, with its benign path/query/SPA fragment preserved.

    The proxy still validates DNS and private addresses at runtime. This check is an earlier
    privacy boundary: rejected values must not enter a prompt or a persisted learning intent.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > MAX_ADDRESS or re.search(r"[\s\\\x00-\x1f\x7f]", raw):
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        host = normalize_host(parts.hostname or "")
        if (
            parts.scheme != "https"
            or not host
            or is_ip_literal(host)
            or "." not in host
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
        ):
            return None
        # A hash route may contain its own query, including an OAuth callback.
        fragment_query = (
            parts.fragment.partition("?")[2] if "?" in parts.fragment else parts.fragment
        )
        for query in (parts.query, fragment_query):
            for key, item in parse_qsl(query, keep_blank_values=True):
                if secret_query_key(key) or _secret(item):
                    return None
        if _secret(unquote(parts.path)) or _secret(unquote(parts.fragment)):
            return None
    except (ValueError, UnicodeError):
        return None
    return urlunsplit(("https", host, parts.path or "/", parts.query, parts.fragment))


def _secret(value: str) -> bool:
    return JWT.search(value) is not None or LONG_TOKEN.search(value) is not None


def url_origin(url: str) -> str:
    """The origin of an already validated navigation URL."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
