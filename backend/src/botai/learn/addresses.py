"""User-supplied navigation addresses, kept distinct from a recipe's single origin.

An entry query can select a week or a view. It must survive observation unchanged; it is not a
license to save a signed URL or send an authentication callback to the model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from botai.browser.proxy import is_ip_literal, normalize_host
from botai.learn.redact import JWT, LONG_TOKEN, secret_query_key
from botai.sources import templates

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


# --- interface addresses ----------------------------------------------------------------------
# A shared recipe may carry the page address of a student's schedule, never a piece of the student:
# a login, a surname, a group code or an account number can look like an ordinary word.

UI_WORDS = frozenset(
    {
        "schedule", "timetable", "calendar", "my", "student", "students", "course", "courses",
        "lesson", "lessons", "class", "classes", "view", "all", "actual", "week", "weekly",
        "day", "month", "list", "current", "today", "home", "main", "portal", "cabinet",
        "app", "tasks", "deadlines", "grades", "news", "faq", "raspisanie", "kabinet", "lk",
    }
)  # fmt: skip
_AUTH_PATH = re.compile(
    r"(?i)(?:^|[/_.-])(?:log-?in|sign-?in|sso|oauth2?|openid|auth|authorize|password|passwd|"
    r"callback|cb|logout|keycloak|realms|consent|mfa|otp|2fa|verify)(?:$|[/_.-])"
)
_RETURN_KEYS = frozenset(
    {"next", "redirect", "redirect_uri", "return", "returnurl", "return_to", "continue", "state"}
    | {"code", "session_code", "ticket"}
)


def structural_word(segment: str) -> bool:
    """A path segment or query value made only of known structural words (`my-schedule`)."""
    parts = [part for part in re.split(r"[-_]", segment.lower()) if part]
    return bool(parts) and all(part in UI_WORDS for part in parts)


def looks_like_auth_page(url: str) -> bool:
    """A sign-in step or callback is never where a student looks at their data."""
    parts = urlsplit(url)
    keys = {key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    return bool(_AUTH_PATH.search(unquote(parts.path))) or bool(keys & _RETURN_KEYS)


def request_literals(
    request: Mapping[str, Any],
) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """Path segments and query literals the recipe's own API request states (templates removed).

    The page address may repeat such a word: the recipe already carries it, so it adds nothing
    that belongs to one student."""
    words = frozenset(
        segment.lower()
        for segment in unquote(templates.PLACEHOLDER.sub("", str(request.get("path", "")))).split(
            "/"
        )
        if segment
    )
    query = request.get("query")
    values: dict[str, frozenset[str]] = {}
    for key, raw in (query if isinstance(query, dict) else {}).items():
        literal = templates.PLACEHOLDER.sub("", raw) if isinstance(raw, str) else ""
        if literal:
            values[str(key)] = frozenset({literal})
    return words, values


def ui_segment_ok(segment: str, request_words: frozenset[str]) -> bool:
    return structural_word(segment) or segment.lower() in request_words


def ui_value_ok(key: str, value: str, request_values: Mapping[str, frozenset[str]]) -> bool:
    """A query literal of an interface address: a structural word or the recipe's own literal."""
    return structural_word(value) or value in request_values.get(key, frozenset())
