"""What is kept of a calendar link the student typed into the protected field.

`webcal://` is `https://` by another name, and the EgressProxy takes nothing but HTTPS on 443, so a
link is stored as `https://host/path?query` (no port, no fragment, the host as plain ASCII). A
`http://` link is upgraded rather than refused: a service that cannot answer on HTTPS fails the check
at once, with the reason. Every refusal says what is wrong and never repeats the link.
"""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit

from botai.browser.proxy import is_ip_literal, normalize_host
from botai.core.errors import BotaiError, ErrorCode

MAX_LINK = 2000
# scheme -> (the scheme that is kept, the port that scheme means by itself)
_SCHEMES = {
    "https": ("https", 443),
    "http": ("https", 80),
    "webcal": ("https", 80),
    "webcals": ("https", 443),
}
_NOT_A_LINK = "Нужна ссылка на экспорт календаря: https:// или webcal://"


def _refuse(text: str) -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, text)


def _public_host(raw: str) -> str:
    """The host as the proxy will see it: ASCII, a real public name (an IDN becomes punycode)."""
    try:
        ascii_host = raw.strip().lower().rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        raise _refuse(_NOT_A_LINK) from None
    host = normalize_host(ascii_host)
    if host is None or is_ip_literal(host):
        raise _refuse("Нужна ссылка на календарь в интернете, а не адрес из сети")
    if "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise _refuse("Нужна ссылка на календарь в интернете, а не адрес из сети")
    return host


def normalize_calendar_link(value: str) -> str:
    """The address to keep in the Vault: `https://host/path?query`. Raises a validation error."""
    raw = value.strip().strip("<>\"'`")
    if not raw or any(ch.isspace() for ch in raw):
        raise _refuse("В поле должна быть одна ссылка на календарь")
    if len(raw) > MAX_LINK:
        raise _refuse("Ссылка слишком длинная")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise _refuse(_NOT_A_LINK) from None
    scheme = _SCHEMES.get(parts.scheme.lower())
    if scheme is None or not parts.hostname:
        raise _refuse(_NOT_A_LINK)
    if parts.username is not None or parts.password is not None:
        raise _refuse("В ссылке не должно быть логина и пароля")
    kept, default_port = scheme
    if port not in (None, default_port):
        raise _refuse("Ссылка с нестандартным портом не подходит")
    host = _public_host(parts.hostname)
    return SplitResult(kept, host, parts.path or "/", parts.query, "").geturl()


def origin_of_link(link: str) -> str:
    """`https://host` of a normalised link: the only part of it that is not a secret."""
    return f"https://{urlsplit(link).hostname or ''}"
