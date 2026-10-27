"""Logging with redaction. Secrets must never reach logs (NFR-013, security.md T10)."""

from __future__ import annotations

import logging
import re
from typing import Any

_SENSITIVE_KEYS = re.compile(
    r"(pass(word)?|secret|token|cookie|authorization|set-cookie|session|otp|code|kek|api[_-]?key)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)bearer\s+[a-z0-9._\-]+")
_JWT = re.compile(r"eyJ[a-zA-Z0-9_\-]{8,}\.[a-zA-Z0-9_\-]{8,}\.[a-zA-Z0-9_\-]{8,}")
REDACTED = "[REDACTED]"


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact mappings by key and strings by pattern."""
    if _depth > 6:
        return REDACTED
    if isinstance(value, dict):
        return {
            k: (
                REDACTED
                if isinstance(k, str) and _SENSITIVE_KEYS.search(k)
                else redact(v, _depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return _JWT.sub(REDACTED, _BEARER.sub("Bearer " + REDACTED, value))
    return value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, dict):
            record.args = redact(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(redact(a) for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
