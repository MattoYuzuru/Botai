"""Public query minimization. Closed pages, history and tool results are never query inputs."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from botai.agent.safety import mask_injections, scrub_secrets
from botai.browser.proxy import is_ip_literal
from botai.learn.addresses import navigation_url
from botai.learn.redact import EMAIL, FIO_FULL, FIO_INITIALS, LONG_DIGITS, LONG_TOKEN, PHONE, UUID
from botai.spaces.catalog import identify

_WORD = re.compile(r"[\w.-]+", re.UNICODE)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_PRIVATE = re.compile(
    r"(?i)\b(?:мо[йяиеёю]\w*|я|мне|меня|наш\w*|I|me|we|our|my|mine|password|cookie|"
    r"authorization|bearer|token|secret|session\w*|парол\w*|токен\w*|куки|сесси\w*|"
    r"mfa|otp|sms|смс|личн\w*|переписк\w*|passport|паспорт\w*)\b"
)
_INSTITUTION = re.compile(r"(?i)университет|институт|академи|university|college|institute")
_PUBLIC_EXTRA = frozenset(
    [
        "официальный",
        "сайт",
        "портал",
        "документация",
        "обучение",
        "university",
        "official",
        "website",
        "portal",
        "documentation",
    ]
)


def public_query(value: object, *, max_chars: int = 128) -> str | None:
    """Reject rather than partly forward anything with private or credential-shaped material."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not 2 <= len(value) <= max_chars or re.search(r"[^\w\s.,?!\"'():\-]", value):
        return None
    if _PRIVATE.search(value) or _URL.search(value):
        return None
    if any(
        p.search(value)
        for p in (EMAIL, PHONE, UUID, FIO_FULL, FIO_INITIALS, LONG_DIGITS, LONG_TOKEN)
    ):
        return None
    clean, findings = mask_injections(value)
    if findings or clean != value or scrub_secrets(value, typed_by_user=True) != value:
        return None
    return value


def institutional_search_query(title: str, domain: str | None = None) -> str | None:
    """Only a canonical name, plainly institutional title or validated public DNS name.

    In particular, arbitrary source titles and user notes do not become external queries.
    """
    entry = identify(title)
    if entry:
        return public_query(f"{entry.title[:90]} официальный сайт портал")
    clean = public_query(title)
    if clean and len(clean) <= 90 and _INSTITUTION.search(clean):
        return public_query(f"{clean} официальный сайт портал")
    if domain and safe_result_url(f"https://{domain}/"):
        return public_query(f"site:{domain} официальный сайт документация")
    return None


def chat_public_query(query: object, question: str) -> str | None:
    """Allow only public words from the current question, never from history or closed tools.

    This deliberately refuses a question containing a secret or personal context instead of
    asking the model to redact it. The model may select short keywords, not copy a message.
    """
    candidate = public_query(query)
    if candidate is None or public_query(question, max_chars=2000) is None:
        return None
    words = {w.casefold() for w in _WORD.findall(question)} | _PUBLIC_EXTRA
    if any(w.casefold() not in words for w in _WORD.findall(candidate)):
        return None
    return candidate


def safe_result_url(value: object) -> str | None:
    """A search hit is a citation/candidate, never a capability or automatic egress grant."""
    url = navigation_url(value)
    if url is None:
        return None
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (
        is_ip_literal(host)
        or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
        or host in {"localhost", "metadata.google.internal"}
    ):
        return None
    # Citation URLs must not carry account identifiers, signed links or personal path values.
    if any(p.search(url) for p in (EMAIL, PHONE, UUID, LONG_DIGITS, LONG_TOKEN)):
        return None
    return url
