"""Compile credential-free replay policies from executor-owned request evidence only.

The caller owns the evidence map. It must never populate it from an author response or a
checkpoint's claimed metadata. Evidence ids are temporary references, not shared recipe fields.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from botai.core.browser import ObservedRequest, auth_origin

_PARAM = re.compile(r"\{(?:[A-Za-z][A-Za-z0-9]{0,30}|(?:plusDays|minusDays):[0-9]{1,3})\}")
# Words that stand for the signed-in student in an address (`/students/me/timetables`). The
# server resolves them by the session; a value from another response is not the same thing and
# the address with it answers "not found".
CURRENT_USER_ALIASES = frozenset({"me", "my", "mine", "own", "self", "current"})


class BindingEvidenceError(ValueError):
    """Only a static reason code may cross into diagnostics or author feedback. `detail` is
    structure only (key names and counts, a segment position), never a value of the source."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str):
        raise BindingEvidenceError(reason)
    return value


def _query(value: object) -> dict[str, list[str]]:
    """The authored query by key; a key that repeats in the address is a list of its values."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BindingEvidenceError("evidence_query_mismatch")
    out: dict[str, list[str]] = {}
    for key, item in value.items():
        items: Sequence[object] = item if isinstance(item, list) else [item]
        out[_text(key, "evidence_query_mismatch")] = [
            _text(one, "evidence_query_mismatch") for one in items
        ]
    return out


def _assign(templates: Sequence[str], observed: Sequence[str]) -> bool:
    """Every authored value stands for a different observed value of the same key. Exact literals
    claim theirs first, so a placeholder takes only what is left."""
    left = list(observed)
    for template in sorted(templates, key=lambda t: "{" in t):
        for index, value in enumerate(left):
            if _matches(template, value):
                del left[index]
                break
        else:
            return False
    return not left


def _key_counts(counts: Counter[str]) -> str:
    return ", ".join(f"{key}×{number}" for key, number in sorted(counts.items())) or "нет"


def _matches(template: str, observed: str, *, path: bool = False) -> bool:
    if len(template) > 2000 or len(observed) > 16_384:
        return False
    if path:
        template, observed = unquote(template), unquote(observed)
        if (
            not template.startswith("/")
            or template.startswith("//")
            or any(part in {".", ".."} for part in template.split("/"))
            or any(char in template for char in ("\\", "?", "#"))
            or any(part in {".", ".."} for part in observed.split("/"))
            or "\\" in observed
        ):
            return False
        expected_parts, actual_parts = template.split("/"), observed.split("/")
        return len(expected_parts) == len(actual_parts) and all(
            _matches(expected, actual)
            for expected, actual in zip(expected_parts, actual_parts, strict=True)
        )
    if "{" in _PARAM.sub("", template) or "}" in _PARAM.sub("", template):
        return False
    params = list(_PARAM.finditer(template))
    if not params:
        return template == observed
    if len(params) != 1:
        return False  # no ambiguous wildcard combinations or generated regular expressions
    param = params[0]
    prefix, suffix = template[: param.start()], template[param.end() :]
    return (
        observed.startswith(prefix)
        and observed.endswith(suffix)
        and len(observed) > len(prefix) + len(suffix)
    )


def _aliases_kept(template: str, observed: str) -> None:
    """A placeholder never takes the place of a word that names the current student."""
    for index, (expected, actual) in enumerate(
        zip(unquote(template).split("/"), unquote(observed).split("/"), strict=False)
    ):
        if _PARAM.search(expected) and actual.casefold() in CURRENT_USER_ALIASES:
            raise BindingEvidenceError(
                "evidence_path_literal_replaced", f"сегмент пути №{index} в наблюдении — «{actual}»"
            )


def compile_auth_binding(
    facts: Mapping[str, ObservedRequest],
    evidence_id: str,
    *,
    origin: str,
    method: str,
    path: str,
    query: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A policy for this observed method/path/query shape, never a credential value.

    Template values may generalize an observed id or time range. Existing recipe lint still
    verifies every placeholder's trusted bootstrap/builtin source and the read-only method.
    """
    evidence_id = _text(evidence_id, "evidence_unknown")
    fact = facts.get(evidence_id)
    if fact is None:
        raise BindingEvidenceError("evidence_unknown")
    if not fact.auth_proven:
        raise BindingEvidenceError("evidence_auth_unproven")
    if fact.status is None or not 200 <= fact.status < 300:
        raise BindingEvidenceError("evidence_response_unverified")
    target = auth_origin(_text(origin, "evidence_origin_mismatch"))
    if target is None or auth_origin(fact.url) != target:
        raise BindingEvidenceError("evidence_origin_mismatch")
    method = _text(method, "evidence_method_mismatch")
    if method not in {"GET", "POST"} or fact.method != method:
        raise BindingEvidenceError("evidence_method_mismatch")
    parts = urlsplit(fact.url)
    if not _matches(_text(path, "evidence_path_mismatch"), parts.path or "/", path=True):
        raise BindingEvidenceError("evidence_path_mismatch")
    _aliases_kept(path, parts.path or "/")
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=32)
    except ValueError as exc:
        raise BindingEvidenceError("evidence_query_mismatch") from exc
    observed: dict[str, list[str]] = {}
    for key, value in pairs:
        observed.setdefault(key, []).append(value)
    requested = _query(query)
    # The keys form a multiset: a repeated key must come back as many times as it was sent.
    seen_keys = Counter({key: len(values) for key, values in observed.items()})
    wanted_keys = Counter({key: len(values) for key, values in requested.items()})
    if seen_keys != wanted_keys:
        raise BindingEvidenceError(
            "evidence_query_mismatch",
            f"в наблюдённом запросе ключи query: {_key_counts(seen_keys)}; "
            f"в описании: {_key_counts(wanted_keys)}",
        )
    if any(not _assign(values, observed[key]) for key, values in requested.items()):
        raise BindingEvidenceError(
            "evidence_query_mismatch", "значения query не совпали с наблюдёнными"
        )
    if fact.has_authorization:
        if fact.authorization_scheme != "bearer":
            raise BindingEvidenceError("evidence_auth_scheme_unsupported")
    elif fact.authorization_scheme != "none":
        raise BindingEvidenceError("evidence_auth_inconsistent")
    return {
        "cookies": fact.has_cookie,
        "bearer": "observedAuthorization" if fact.has_authorization else "none",
    }
