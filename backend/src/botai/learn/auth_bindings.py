"""Compile credential-free replay policies from executor-owned request evidence only.

The caller owns the evidence map. It must never populate it from an author response or a
checkpoint's claimed metadata. Evidence ids are temporary references, not shared recipe fields.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from botai.core.browser import ObservedRequest, auth_origin

_PARAM = re.compile(r"\{(?:[A-Za-z][A-Za-z0-9]{0,30}|(?:plusDays|minusDays):[0-9]{1,3})\}")


class BindingEvidenceError(ValueError):
    """Only a static reason code may cross into diagnostics or author feedback."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str):
        raise BindingEvidenceError(reason)
    return value


def _query(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BindingEvidenceError("evidence_query_mismatch")
    return {
        _text(key, "evidence_query_mismatch"): _text(item, "evidence_query_mismatch")
        for key, item in value.items()
    }


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
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=32)
    except ValueError as exc:
        raise BindingEvidenceError("evidence_query_mismatch") from exc
    observed = dict(pairs)
    requested = _query(query)
    if len(pairs) != len(observed) or set(requested) != set(observed):
        raise BindingEvidenceError("evidence_query_mismatch")
    if any(not _matches(value, observed[key]) for key, value in requested.items()):
        raise BindingEvidenceError("evidence_query_mismatch")
    if fact.has_authorization:
        if fact.authorization_scheme != "bearer":
            raise BindingEvidenceError("evidence_auth_scheme_unsupported")
    elif fact.authorization_scheme != "none":
        raise BindingEvidenceError("evidence_auth_inconsistent")
    return {
        "cookies": fact.has_cookie,
        "bearer": "observedAuthorization" if fact.has_authorization else "none",
    }
