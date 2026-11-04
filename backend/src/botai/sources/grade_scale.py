"""The number and the top of the scale of a grade, only when the source printed both plainly.

D-039: the application never averages, converts between scales or guesses. A grade gets
`value_num`/`scale_max` when its text says, without ambiguity, "this number out of that scale":

* the value is a plain number (`8`, `8,5`, `8.5`) and the scale names its top (`10`, `из 10`,
  `10 баллов`, `10-балльная`, `0-10`, `шкала 100`);
* or the value carries its own scale (`8/10`, `8 из 10`, `86%`).

Anything else ("зачтено", "отлично", `B+`, a number without a scale, a number above its scale, two
scales that disagree) gives `(None, None)`: the grade is still shown exactly as the source printed
it, it just has no place on a chart.
"""

from __future__ import annotations

import math
import re
from typing import Any

_NUM = r"\d{1,6}(?:[.,]\d{1,3})?"
_POINTS = r"(?:балл(?:ов|а)?|б\.?|pts?|points?)"
_PLAIN = re.compile(rf"^\s*(?P<n>{_NUM})\s*$")
_PERCENT = re.compile(rf"^\s*(?P<n>{_NUM})\s*%\s*$")
_OF = re.compile(
    rf"^\s*(?P<n>{_NUM})\s*(?:/|из|of)\s*(?P<m>{_NUM})\s*(?:{_POINTS})?\s*$", re.IGNORECASE
)
_SCALE_TOP = re.compile(
    rf"^\s*(?:(?:шкала|scale)\s*:?\s*)?(?:(?:из|of|/)\s*)?(?P<m>{_NUM})\s*(?:{_POINTS})?\s*$",
    re.IGNORECASE,
)
_SCALE_ADJECTIVE = re.compile(
    rf"^\s*(?P<m>{_NUM})\s*-?\s*балльн\w*(?:\s+шкала)?\s*$", re.IGNORECASE
)
_SCALE_RANGE = re.compile(
    rf"^\s*(?:(?:шкала|scale)\s*:?\s*)?(?:от\s*)?0\s*(?:-|–|—|\.\.|до|to)\s*(?P<m>{_NUM})"
    rf"\s*(?:{_POINTS})?\s*$",
    re.IGNORECASE,
)


def _number(raw: str) -> float | None:
    try:
        number = float(raw.replace(",", "."))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def plain_number(value: Any) -> float | None:
    """A number the source printed on its own (`92,5`, `8`); None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if not isinstance(value, str):
        return None
    found = _PLAIN.match(value)
    return _number(found["n"]) if found else None


def _value_parts(text: str) -> tuple[float | None, float | None]:
    """The number of a value and the top of the scale the value itself carries, if it does."""
    if (found := _PLAIN.match(text)) is not None:
        return _number(found["n"]), None
    if (found := _PERCENT.match(text)) is not None:
        return _number(found["n"]), 100.0
    if (found := _OF.match(text)) is not None:
        return _number(found["n"]), _number(found["m"])
    return None, None


def scale_top(text: str) -> float | None:
    """The top of a scale written as `10`, `из 10`, `10 баллов`, `10-балльная`, `0-10`."""
    for pattern in (_SCALE_TOP, _SCALE_ADJECTIVE, _SCALE_RANGE):
        if (found := pattern.match(text)) is not None:
            return _number(found["m"])
    return None


def _as_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = int(value) if value.is_integer() else value
    text = str(value).strip()
    return text or None


def grade_numbers(value: Any, scale: Any) -> tuple[float | None, float | None]:
    """`(value_num, scale_max)` when the grade says both unambiguously, else `(None, None)`."""
    value_text = _as_text(value)
    if value_text is None:
        return None, None
    number, own_top = _value_parts(value_text)
    if number is None:
        return None, None
    scale_text = _as_text(scale)
    named_top = scale_top(scale_text) if scale_text is not None else None
    if own_top is not None and named_top is not None and own_top != named_top:
        return None, None  # two scales that disagree: not unambiguous
    top = own_top if own_top is not None else named_top
    if top is None or top <= 0 or number < 0 or number > top:
        return None, None
    return number, top
