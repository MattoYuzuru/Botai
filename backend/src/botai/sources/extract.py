"""Reading values out of responses: JSON Pointer (RFC 6901), CSS selectors, transforms.

Everything here is deterministic and works on data only; nothing in a response can change what
is executed. Values are cleaned (control characters removed, length bounded) before they leave.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time
from typing import Any, Final
from zoneinfo import ZoneInfo

from selectolax.lexbor import LexborHTMLParser, LexborNode

from botai.sources import rxsafe

MAX_TEXT = 20_000


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _Missing()


# --- JSON Pointer ---------------------------------------------------------------------------


def resolve(doc: Any, pointer: str) -> Any:
    """Value at `pointer` or MISSING. Empty pointer is the document itself."""
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        return MISSING
    cur: Any = doc
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if token not in cur:
                return MISSING
            cur = cur[token]
        elif isinstance(cur, list):
            if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
                return MISSING
            idx = int(token)
            if idx >= len(cur):
                return MISSING
            cur = cur[idx]
        else:
            return MISSING
    return cur


# --- cleaning -------------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


def clean_text(value: str, limit: int = MAX_TEXT) -> str:
    return _CONTROL.sub("", value)[:limit]


def collapse(value: str) -> str:
    return _SPACES.sub(" ", value).strip()


# --- dates ----------------------------------------------------------------------------------

_RU_NUMERIC = re.compile(
    r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?:[ T,]+(\d{1,2})[:.](\d{2})(?::(\d{2}))?)?$"
)
_RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_RU_TEXT = re.compile(
    r"^(\d{1,2})\s+([а-яё]+)\s+(\d{4})(?:\s*(?:г\.?|года)?)?(?:[ ,]+(\d{1,2})[:.](\d{2}))?$"
)
_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})(?::(\d{2}))?$")


def parse_datetime(value: Any, tz: ZoneInfo) -> datetime | None:
    """Aware datetime from an ISO string, a Russian-style date or a datetime; naive means `tz`."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = collapse(value)
        if not text:
            return None
        dt = _parse_text(text)
        if dt is None:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def _parse_text(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    m = _RU_NUMERIC.match(text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        hh, mm, ss = int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0)
        return _safe_dt(y, mo, d, hh, mm, ss)
    m = _RU_TEXT.match(text.lower())
    if m and m.group(2) in _RU_MONTHS:
        return _safe_dt(
            int(m.group(3)),
            _RU_MONTHS[m.group(2)],
            int(m.group(1)),
            int(m.group(4) or 0),
            int(m.group(5) or 0),
            0,
        )
    return None


def _safe_dt(y: int, mo: int, d: int, hh: int, mm: int, ss: int) -> datetime | None:
    try:
        return datetime(y, mo, d, hh, mm, ss)
    except ValueError:
        return None


def parse_time(value: Any) -> time | None:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None)
    if isinstance(value, str):
        m = _TIME.match(collapse(value))
        if m:
            try:
                return time(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
            except ValueError:
                return None
    return None


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


# --- transforms -----------------------------------------------------------------------------

_NUMBER = re.compile(r"-?\d[\d\s]*(?:[.,]\d+)?")
_TRUE = {"true", "1", "yes", "y", "да", "истина", "on"}
_FALSE = {"false", "0", "no", "n", "нет", "ложь", "off", ""}
_NAME_KEYS = ("name", "fullName", "fullname", "displayName", "title", "label")
_UNIX_MIN, _UNIX_MAX = 946_684_800, 4_102_444_800  # 2000-01-01 .. 2100-01-01


def html_to_text(fragment: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|ul|ol|table)>", "\n", text)
    parsed = LexborHTMLParser(f"<body>{text}</body>")
    for bad in parsed.css("script,style"):
        bad.decompose()
    body = parsed.body
    raw = body.text(separator="", strip=False) if body is not None else ""
    lines = [collapse(line) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def _to_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        m = _NUMBER.search(value)
        if not m:
            return None
        text = re.sub(r"\s+", "", m.group(0)).replace(",", ".")
        try:
            num = float(text)
        except ValueError:
            return None
        return int(num) if num.is_integer() else num
    return None


def _join_names(value: Any) -> str | None:
    if isinstance(value, str):
        return collapse(value) or None
    if not isinstance(value, list):
        return None
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(collapse(item))
        elif isinstance(item, dict):
            for key in _NAME_KEYS:
                got = item.get(key)
                if isinstance(got, str) and got.strip():
                    names.append(collapse(got))
                    break
    names = [n for n in names if n]
    return ", ".join(names) if names else None


def _unix(value: Any, divisor: int, tz: ZoneInfo) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        seconds = float(value) / divisor
    except ValueError:
        return None
    if not _UNIX_MIN <= seconds <= _UNIX_MAX:
        return None
    return to_utc_iso(datetime.fromtimestamp(seconds, tz=UTC))


def apply_transform(name: str | None, value: Any, tz: ZoneInfo) -> Any:
    """Apply a recipe transform. Returns None when the value cannot be interpreted."""
    if value is MISSING or value is None:
        return None
    if name is None:
        return _scalar(value)
    if name == "iso":
        dt = parse_datetime(value, tz)
        return to_utc_iso(dt) if dt else None
    if name == "unixSeconds":
        return _unix(value, 1, tz)
    if name == "unixMillis":
        return _unix(value, 1000, tz)
    if name == "trim":
        return clean_text(collapse(value)) if isinstance(value, str) else _scalar(value)
    if name == "number":
        return _to_number(value)
    if name == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
        return None
    if name == "html2text":
        return clean_text(html_to_text(value)) if isinstance(value, str) else _scalar(value)
    if name == "joinNames":
        joined = _join_names(value)
        return clean_text(joined) if joined else None
    return None


def _scalar(value: Any) -> Any:
    """Only scalars flow on: a nested object or list is not a field value."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, (bool, int, float)):
        return value
    return None


# --- HTML -----------------------------------------------------------------------------------


def parse_html(html: str) -> LexborHTMLParser:
    return LexborHTMLParser(html)


def select_items(tree: LexborHTMLParser, selector: str) -> list[LexborNode]:
    try:
        return list(tree.css(selector))
    except Exception:
        return []


def selector_valid(selector: str) -> bool:
    try:
        LexborHTMLParser("<div></div>").css(selector)
    except Exception:
        return False
    return True


def node_text(node: LexborNode) -> str:
    return collapse(node.text(separator=" ", strip=True))


def html_value(
    item: LexborNode,
    *,
    selector: str,
    attr: str | None,
    pattern: str | None,
    transform: str | None,
    tz: ZoneInfo,
) -> Any:
    """One field of one list element. `selector` '.' is the element itself."""
    target = item if selector == "." else item.css_first(selector)
    if target is None:
        return None
    raw: Any
    if attr:
        raw = target.attributes.get(attr)
        if raw is None:
            return None
    elif transform == "html2text":
        raw = target.html or ""
    else:
        raw = node_text(target)
    if pattern and isinstance(raw, str) and not attr:
        raw = rxsafe.first_group(pattern, raw)
        if raw is None:
            return None
    if isinstance(raw, str) and transform != "html2text":
        raw = clean_text(collapse(raw))
    return apply_transform(transform, raw, tz)
