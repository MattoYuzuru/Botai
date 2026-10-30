"""Links to a calendar export (iCal): how to recognise one, and how to make one fit to store.

Yandex, Google and Outlook calendars hand out a personal export link with the access key inside
the address (in the query, or in the path). Whoever has the link reads the whole calendar, so it is
a secret of its owner: it travels only in the protected field of a task, then lives in the Vault
(`ical_url`). Nothing here knows a university; the rules are those of the calendar formats and of
the big calendar services.

The functions never echo a link back in an error: a refusal says what is wrong, not what was sent.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import SplitResult, unquote, urlsplit

from botai.core.errors import BotaiError, ErrorCode

MAX_LINK = 2000
_SCHEMES = {"https": "https", "http": "https", "webcal": "https", "webcals": "https"}
# Calendar services whose personal export address is an `.ics` of their own making.
_SERVICE_HOST = re.compile(
    r"(?i)^(?:calendar\.yandex\.[a-z.]+|calendar\.google\.com|outlook\.(?:live|office|office365)\.com"
    r"|[\w-]+\.outlook\.com|caldav\.icloud\.com|p\d+-caldav\.icloud\.com)$"
)
_EXPORT_FILE = (".ics", ".ical", ".ifb")
_EXPORT_QUERY = re.compile(r"(?i)(?:^|[&=])(?:ics|ical)(?:$|[&.=])")
_ICAL_SEGMENT = re.compile(r"(?i)(?:^|/)ical(?:/|$)")
_LINK = re.compile(
    r"(?i)\b(?:webcals?|https?)://[^\s<>\"'`]+"
    r"|(?<![\w@./:-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?/[^\s<>\"'`]+"
)
_TRAILING = ".,;:!?)]}»"
_NOT_A_LINK = "Нужна ссылка на экспорт календаря: https:// или webcal://"


def _is_export(parts: SplitResult) -> bool:
    if parts.scheme.lower() in {"webcal", "webcals"}:
        return True
    path = unquote(unquote(parts.path)).lower()
    name = path.rsplit("/", 1)[-1]
    if name.endswith(_EXPORT_FILE) or name == "ics.xml" or "/export/ics" in path:
        return True
    if _ICAL_SEGMENT.search(path):
        return True
    query = unquote(parts.query)
    service = bool(_SERVICE_HOST.match((parts.hostname or "").lower()))
    return bool(_EXPORT_QUERY.search(query)) or (service and ("ics" in path or "ical" in path))


def hide_calendar_links(text: str, replacement: str) -> str:
    """The text with every calendar export link in it replaced (for logs and error texts)."""

    def hide(found: re.Match[str]) -> str:
        link = found.group(0).rstrip(_TRAILING)
        return (
            replacement + found.group(0)[len(link) :] if is_calendar_link(link) else found.group(0)
        )

    return _LINK.sub(hide, text)


def is_calendar_link(url: str) -> bool:
    """Whether one address is a calendar export: `webcal://`, an `.ics` file or `/export/ics`,
    an `/ical/` path, or an `ics` address of a calendar service."""
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        return _is_export(urlsplit(raw))
    except ValueError:
        return False


def calendar_link_in(text: str) -> bool:
    """Whether a message carries a calendar export link (with or without `https://`)."""
    return any(is_calendar_link(found.group(0).rstrip(_TRAILING)) for found in _LINK.finditer(text))


def _public_host(host: str) -> bool:
    if (
        "." not in host
        or host in {"localhost"}
        or host.endswith((".local", ".localhost", ".internal"))
    ):
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return True
    return False


def normalize_calendar_link(value: str) -> str:
    """The address to keep in the Vault: `https://host/path?query`.

    `webcal://` is `https://` by another name, and the EgressProxy takes nothing but HTTPS on 443,
    so `http://` is upgraded rather than refused: a service that cannot answer on HTTPS fails the
    check at once, with the reason. Raises a validation error that does not repeat the link.
    """
    raw = value.strip().strip("<>\"'`")
    if not raw or any(ch.isspace() for ch in raw):
        raise BotaiError(ErrorCode.VALIDATION, "В поле должна быть одна ссылка на календарь")
    if len(raw) > MAX_LINK:
        raise BotaiError(ErrorCode.VALIDATION, "Ссылка слишком длинная")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise BotaiError(ErrorCode.VALIDATION, _NOT_A_LINK) from None
    scheme = _SCHEMES.get(parts.scheme.lower())
    host = (parts.hostname or "").lower()
    if scheme is None or not host:
        raise BotaiError(ErrorCode.VALIDATION, _NOT_A_LINK)
    if parts.username is not None or parts.password is not None:
        raise BotaiError(ErrorCode.VALIDATION, "В ссылке не должно быть логина и пароля")
    if not _public_host(host):
        raise BotaiError(
            ErrorCode.VALIDATION, "Нужна ссылка на календарь в интернете, а не адрес из сети"
        )
    if port not in (None, 443, 80):
        raise BotaiError(ErrorCode.VALIDATION, "Ссылка с нестандартным портом не подходит")
    return SplitResult(scheme, host, parts.path or "/", parts.query, "").geturl()


def origin_of_link(link: str) -> str:
    """`https://host` of a normalised link: the only part of it that is not a secret."""
    return f"https://{urlsplit(link).hostname or ''}"
