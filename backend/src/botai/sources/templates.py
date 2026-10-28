"""Request templates of a recipe: `{today}`, `{nowUnix}`, `{plusDays:N}`, `{minusDays:N}`, `{param}`.

`param` values come from the recipe's bootstrap steps (read from the user's own session at run
time), never from the spec. When a value is substituted into a URL path it is percent-encoded
completely, so a hostile response cannot smuggle `../` or a second host into the next request.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_DAYS = re.compile(r"^(plusDays|minusDays):(\d{1,4})$")
PARAM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,30}$")
BUILTIN_NAMES = frozenset({"today", "nowUnix", "weekStart"})
MAX_PARAM_VALUE = 200


class TemplateError(Exception):
    """A placeholder cannot be filled. `name` is the placeholder, never a value."""

    def __init__(self, name: str) -> None:
        super().__init__(f"cannot fill placeholder {name!r}")
        self.name = name


def placeholders(template: str) -> list[str]:
    return PLACEHOLDER.findall(template)


def is_builtin(name: str) -> bool:
    return name in BUILTIN_NAMES or _DAYS.match(name) is not None


def is_valid_placeholder(name: str, known_params: frozenset[str] | set[str]) -> bool:
    return is_builtin(name) or (PARAM_NAME.match(name) is not None and name in known_params)


def _builtin(name: str, now: datetime, tz: ZoneInfo) -> str | None:
    local = now.astimezone(tz)
    if name == "today":
        return local.date().isoformat()
    if name == "weekStart":
        return (local.date() - timedelta(days=local.weekday())).isoformat()
    if name == "nowUnix":
        return str(int(now.timestamp()))
    m = _DAYS.match(name)
    if m:
        days = int(m.group(2))
        delta = timedelta(days=days if m.group(1) == "plusDays" else -days)
        return (local + delta).date().isoformat()
    return None


def render(
    template: str,
    *,
    now: datetime,
    tz: ZoneInfo,
    params: Mapping[str, str],
    in_path: bool = False,
) -> str:
    """Fill every placeholder; unknown or unset ones raise TemplateError."""

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        value = _builtin(name, now, tz)
        if value is None:
            if name not in params:
                raise TemplateError(name)
            value = params[name]
        if in_path:
            if value in {".", ".."}:
                raise TemplateError(name)  # a dot segment would climb the path
            return quote(value, safe="")
        return value

    return PLACEHOLDER.sub(fill, template)
