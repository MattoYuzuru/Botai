"""Restricted regular expressions for specs written by a model.

Python's `re` has no timeout, so a catastrophic pattern would stall the worker. A pattern is
accepted only when it has no nested repeats, no alternation inside a repeat, no backreferences
and no lookarounds, and at most `MAX_UNBOUNDED` unbounded repeats; matching runs on a bounded
input (`MAX_INPUT`). With these limits the worst case is polynomial on a short string.
"""

from __future__ import annotations

import re
from typing import Any

MAX_PATTERN = 200
MAX_UNBOUNDED = 3
MAX_INPUT = 300
_MAXREPEAT = 65535

_REPEATS = {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}


def _name(op: Any) -> str:
    return str(getattr(op, "name", op))


def _walk(parsed: Any, *, in_repeat: bool, counter: list[int], reasons: set[str]) -> bool:
    """Returns True if the subtree contains a repeat."""
    has_repeat = False
    for op, av in parsed:
        name = _name(op)
        if name in _REPEATS:
            _lo, hi, body = av
            unbounded = hi >= _MAXREPEAT or hi > 1000
            if unbounded:
                counter[0] += 1
            multi = unbounded or hi > 1
            if in_repeat and multi:
                reasons.add("вложенные повторы")
            _walk(body, in_repeat=in_repeat or multi, counter=counter, reasons=reasons)
            has_repeat = True
        elif name == "SUBPATTERN":
            inner = _walk(av[3], in_repeat=in_repeat, counter=counter, reasons=reasons)
            has_repeat = has_repeat or inner
        elif name == "ATOMIC_GROUP":
            inner = _walk(av, in_repeat=in_repeat, counter=counter, reasons=reasons)
            has_repeat = has_repeat or inner
        elif name == "BRANCH":
            if in_repeat:
                reasons.add("альтернатива внутри повтора")
            for branch in av[1]:
                inner = _walk(branch, in_repeat=in_repeat, counter=counter, reasons=reasons)
                has_repeat = has_repeat or inner
        elif name in {"GROUPREF", "GROUPREF_EXISTS"}:
            reasons.add("обратная ссылка")
        elif name in {"ASSERT", "ASSERT_NOT"}:
            reasons.add("lookaround")
    return has_repeat


def problems(pattern: str) -> list[str]:
    """Reasons the pattern is not acceptable; empty means it is. Never includes the pattern."""
    if len(pattern) > MAX_PATTERN:
        return ["слишком длинное выражение"]
    try:
        import re._parser as sre_parser  # type: ignore[import-not-found,unused-ignore]

        parsed = sre_parser.parse(pattern)  # pyright: ignore[reportUnknownVariableType]
        re.compile(pattern)
    except (re.error, RecursionError, OverflowError):
        return ["некорректное выражение"]
    counter = [0]
    reasons: set[str] = set()
    _walk(parsed, in_repeat=False, counter=counter, reasons=reasons)
    if counter[0] > MAX_UNBOUNDED:
        reasons.add("слишком много неограниченных повторов")
    return sorted(reasons)


def first_group(pattern: str, text: str) -> str | None:
    """First capture group (or the whole match). The pattern must have passed `problems`."""
    match = re.search(pattern, text[:MAX_INPUT])
    if match is None:
        return None
    if match.re.groups:
        return match.group(1)
    return match.group(0)
