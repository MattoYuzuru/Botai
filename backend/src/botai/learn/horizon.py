"""The horizon of a recipe: how many windows of dates it reads (FR-039, FR-040).

A source often answers one window from a date: a week, a month. A recipe that reads only the
window of today sees a thin or an empty answer around a holiday or a control week, while the
neighbouring windows hold the real schedule. These helpers decide, from the spec alone, whether
an endpoint moves a date, and build the same endpoint as a bounded walk over neighbouring windows.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from botai.sources import templates

SPARSE = 2  # this many records or fewer over the whole horizon is suspiciously thin
DEFAULT_WINDOWS = 6
DEFAULT_START = -1  # the previous window, the current one and four more
DEFAULT_STEP = 7
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def date_keys(endpoint: Mapping[str, Any]) -> list[str]:
    """Query keys of the request whose value is a calendar-date template."""
    query = (endpoint.get("request") or {}).get("query") or {}
    return [
        key
        for key, value in query.items()
        if isinstance(value, str) and templates.is_date_template(value)
    ]


def is_walk(endpoint: Mapping[str, Any]) -> bool:
    return (endpoint.get("pagination") or {}).get("type") == "dateWindow"


def moves_date(endpoint: Mapping[str, Any]) -> bool:
    return is_walk(endpoint) or bool(date_keys(endpoint))


def _step_days(endpoint: Mapping[str, Any], now: datetime, tz: ZoneInfo) -> int:
    """A week by default; a `from`..`to` pair of templates declares its own span."""
    query = endpoint["request"]["query"]
    rendered = [
        templates.render(query[key], now=now, tz=tz, params={}) for key in date_keys(endpoint)
    ]
    days = sorted(date.fromisoformat(text) for text in rendered if _ISO_DATE.fullmatch(text))
    if len(days) >= 2 and 1 <= (days[-1] - days[0]).days <= 31:
        return (days[-1] - days[0]).days
    return DEFAULT_STEP


def walked(endpoint: Mapping[str, Any], now: datetime, tz: ZoneInfo) -> dict[str, Any] | None:
    """The same endpoint read over `DEFAULT_WINDOWS` windows, or None when it cannot be.

    Only a plain GET that moves a date template and has no other pagination can walk: a rendered
    page, a body, a weekly series or an offset/cursor already own their own traversal.
    """
    request = endpoint.get("request") or {}
    if (
        is_walk(endpoint)
        or endpoint.get("pagination") not in (None, {"type": "none"})
        or request.get("method") != "GET"
        or request.get("render")
        or request.get("browserPlan")
        or (endpoint.get("response") or {}).get("expand")
    ):
        return None
    keys = date_keys(endpoint)
    if not keys:
        return None
    wide = copy.deepcopy(dict(endpoint))
    wide["pagination"] = {
        "type": "dateWindow",
        "param": keys[0],
        "stepDays": _step_days(endpoint, now, tz),
        "windows": DEFAULT_WINDOWS,
        "startOffset": DEFAULT_START,
    }
    return wide


def covered(endpoint: Mapping[str, Any], now: datetime, tz: ZoneInfo) -> tuple[date, date] | None:
    """First and last day the walk of this endpoint can show, in the source's calendar."""
    window = endpoint.get("pagination") or {}
    if window.get("type") != "dateWindow":
        return None
    template = endpoint["request"]["query"][window["param"]]
    step = int(window["stepDays"])
    first = int(window.get("startOffset", 0))
    last = first + int(window["windows"]) - 1

    def render(offset: int) -> date | None:
        moved = templates.shifted_now(now, tz, offset * step)
        text = templates.render(template, now=moved, tz=tz, params={})
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None

    start, end = render(first), render(last)
    if start is None or end is None:
        return None
    return start, end + timedelta(days=step - 1)


def plural(count: int, forms: tuple[str, str, str]) -> str:
    """Russian number agreement: 1 занятие, 2 занятия, 5 занятий."""
    tail = count % 100
    if 11 <= tail <= 14:
        return forms[2]
    return forms[0] if count % 10 == 1 else forms[1] if 2 <= count % 10 <= 4 else forms[2]


def _day(value: date) -> str:
    return f"{value.day:02d}.{value.month:02d}"


def thin_question(
    count: int,
    *,
    kind_forms: tuple[str, str, str],
    span: tuple[date, date] | None,
    per_window: list[int],
    unit: str,
) -> str:
    """The plain-text question about a source that shows almost nothing: what was found, over
    which dates, and what the neighbouring windows hold. It carries counts and dates, no names."""
    noun = plural(count, kind_forms)
    where = f" за {_day(span[0])}–{_day(span[1])}" if span else ""
    found = f"Вижу {count} {noun}{where}." if count else f"Не вижу ни одного занятия{where}."
    if len(per_window) > 1:
        found += f" По {unit}: " + ", ".join(str(n) for n in per_window) + "."
    return (
        f"{found} Это так? Если здесь должно быть больше, пришлите ссылку на страницу, "
        "где видно полное расписание, или поясните, что подключить."
    )
