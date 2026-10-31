"""Masking of primary secrets before a proposal, journal row or tool result is persisted.

D-006/D-026: the journal stores a redacted preview and a `body_sha256`, never the values. Header
and cookie *names* are safe; their values are not kept at all. Values of form fields whose name
looks like a password/token/OTP are replaced with `***`; free text additionally gets bearer/JWT/
key patterns masked, so a token pasted into an "intent" or a raw body does not survive.

This is a defence for the journal, not a detector of every possible secret: primary secrets are
supposed to reach tools only through the secret broker (D-031), and the model should never see
them in the first place.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

MASK = "***"

# Keys whose value is a primary secret or a session credential: the journal never shows them.
SENSITIVE_KEY = re.compile(
    r"(pass(word|wd|phrase)?|secret|пароль|token|api[-_]?key|apikey|access[-_]?key|"
    r"private[-_]?key|cookie|authorization|auth|session|otp|mfa|one[-_]?time|verification|"
    r"csrf|xsrf|pin|credential)",
    re.IGNORECASE,
)

# Free-text patterns: a value pasted into a body/intent is masked even without a telling key name.
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
        MASK,
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|pass|token|secret|api[-_]?key|otp|код)\b"
            r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s&;,]+)"
        ),
        r"\1\2***",
    ),
    # A bot-style or cloud key with a long random tail: `123456:AA...`, `sk-...`, `ghp_...`.
    (re.compile(r"\b\d{6,}:A[A-Za-z0-9_-]{20,}\b"), MASK),
    (re.compile(r"\b(sk|rk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b"), MASK),
)

_MAX_TEXT = 2000


def mask_text(value: str) -> str:
    """Mask obvious secret patterns in free text; the rest is returned unchanged."""
    text = value[:_MAX_TEXT]
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def mask_secrets(value: Any, key: str | None = None) -> Any:
    """Recursively replace values under secret-looking keys with `***`."""
    if key is not None and SENSITIVE_KEY.search(key):
        return MASK
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping):
        return {str(name): mask_secrets(item, str(name)) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [mask_secrets(item) for item in value]
    return value


def public_header_names(headers: Mapping[str, str]) -> list[str]:
    """Header names only: values carry tokens/cookies and are never journaled (D-026)."""
    return sorted({str(name).lower() for name in headers})


def canonical_body(body: Any, body_kind: str) -> str:
    """The exact bytes-to-hash of a body, canonical across dict ordering."""
    if body_kind == "raw":
        return str(body)
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def body_hash(body: Any, body_kind: str) -> str:
    """sha256 of the canonical body; only this digest is journaled, never the body."""
    return hashlib.sha256(canonical_body(body, body_kind).encode()).hexdigest()


__all__ = [
    "MASK",
    "SENSITIVE_KEY",
    "body_hash",
    "canonical_body",
    "mask_secrets",
    "mask_text",
    "public_header_names",
]
