"""Personal calendar export links: how to recognise one in a message or a log line.

Yandex, Google, Outlook, iCloud and others hand out a personal export link with the access key in
the address (in the query, or in the path). Whoever has the link reads the whole calendar, so it is
a secret of its owner: it must not stay in a chat, a log or a request to a model. A link of that
kind has two traits: it looks like an export of a calendar (`webcal://`, an `.ics` file, `/export/ics`,
`format=ics`, the published-calendar paths of the big services) and it carries a key (a long random
part, `private-…`, a parameter named `token` or `key`). A public `.ics` without a key (a holiday
calendar, a page about subscribing) is an ordinary link.

The same rules are written in TypeScript (`apps/web/src/features/chat/calendar-link.ts`) so that
the client keeps such a link out of its queue; `contracts/calendar-link-vectors.json` holds the cases
both implementations must agree on. Nothing here knows a university.
"""

from __future__ import annotations

import re
import unicodedata
from itertools import pairwise
from urllib.parse import SplitResult, parse_qsl, unquote, urlsplit

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
_LINK = re.compile(
    r"(?i)\b(?:webcals?|https?)://[^\s<>\"'`]+"
    r"|(?<![\w@./:-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?/[^\s<>\"'`]+"
)
_TRAILING = ".,;:!?)]}»"
_SPLIT_SCHEME = re.compile(r"(?i)\b(webcals?|https?)\s*:\s*//\s*")
_EXPORT_FILE = (".ics", ".ical", ".ifb")
_EXPORT_QUERY = {"format", "export", "type", "output", "fmt", "download", "feed"}
_KEY_NAME = re.compile(r"(?i)token|key|secret|auth|sig|pass|private|code|sid")
_KEY_WORD = re.compile(r"(?i)(?:^|[-_.])(?:private|secret|token|key|auth)(?:[-_.]|$)")
_KEY_PREFIX = re.compile(r"(?i)^(?:private|secret|token|key|auth)[-_.]\S{2,}")
_SEPARATORS = re.compile(r"[-_.]")
_ICLOUD = re.compile(r"(?i)^(?:p\d+-)?caldav\.icloud\.com$")
_OUTLOOK = re.compile(r"(?i)^outlook\.(?:live|office|office365)\.com$|^[\w-]+\.outlook\.com$")


def _clean(text: str) -> str:
    """The message as a reader sees it: look-alike characters folded (a full-width colon), zero-width
    characters dropped, `https:// host` joined: a link cannot hide in how it is typed."""
    folded = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return _SPLIT_SCHEME.sub(lambda found: found.group(1).lower() + "://", folded)


def _parts(url: str) -> SplitResult | None:
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - a malformed port raises here, not later
    except ValueError:
        return None
    return parts if parts.hostname else None


def _segments(parts: SplitResult) -> list[str]:
    return [unquote(unquote(s)) for s in parts.path.split("/") if s]


def _key_like(value: str) -> bool:
    """A part of an address that is a key: a long unbroken run of letters and digits, a long run of
    anything, or a word like `private-…` / `token`."""
    value = unquote(unquote(value))
    run = max(map(len, _SEPARATORS.split(value)), default=0)
    mixed = any(c.isalpha() for c in value) and any(c.isdigit() for c in value)
    return (
        (run >= 16 and mixed)
        or run >= 32
        or bool(_KEY_PREFIX.match(value))
        or bool(_KEY_WORD.search(value))
    )


def _has_key(parts: SplitResult) -> bool:
    if any(_key_like(segment) for segment in _segments(parts)):
        return True
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    return any((_KEY_NAME.search(name) and value) or _key_like(value) for name, value in pairs)


def _export_shape(parts: SplitResult) -> bool:
    """Whether the address is an export of a calendar (a key aside)."""
    if parts.scheme.lower() in {"webcal", "webcals"}:
        return True
    segments = [s.lower() for s in _segments(parts)]
    if segments and (
        segments[-1].endswith(_EXPORT_FILE) or segments[-1] in {"ics.xml", "export_execute.php"}
    ):
        return True
    for here, after in pairwise(segments):
        if here in {"ical", "ics"} or (here == "export" and after.startswith(("ics", "ical"))):
            return True
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        name, value = name.lower(), value.lower()
        if name in _EXPORT_QUERY and (
            value in {"ics", "ical", "icalendar"} or (name == "export" and not value)
        ):
            return True
    return False


def _published(parts: SplitResult) -> bool:
    """The published-calendar paths of services whose every address is a key."""
    host, path = (parts.hostname or "").lower(), unquote(parts.path).lower()
    return (
        (bool(_ICLOUD.match(host)) and path.startswith("/published/"))
        or (bool(_OUTLOOK.match(host)) and "/owa/calendar/" in path)
        or ("/dav/public-calendars/" in path)
    )


def is_calendar_link(url: str) -> bool:
    """Whether one address is a personal calendar link: an export that carries a key."""
    parts = _parts(unicodedata.normalize("NFKC", url).translate(_ZERO_WIDTH))
    if parts is None:
        return False
    return _published(parts) or (_export_shape(parts) and _has_key(parts))


def calendar_link_in(text: str) -> bool:
    """Whether a message carries a personal calendar link (with or without `https://`)."""
    return any(
        is_calendar_link(found.group(0).rstrip(_TRAILING)) for found in _LINK.finditer(_clean(text))
    )


def hide_calendar_links(text: str, replacement: str) -> str:
    """The text with every personal calendar link in it replaced (for logs and error texts)."""

    def hide(found: re.Match[str]) -> str:
        link = found.group(0).rstrip(_TRAILING)
        return (
            replacement + found.group(0)[len(link) :] if is_calendar_link(link) else found.group(0)
        )

    return _LINK.sub(hide, text)
