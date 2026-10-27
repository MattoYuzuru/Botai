"""The `kind=session` Vault value: `SessionState` as JSON with the secrets revealed.

`SessionState` keeps cookie values and the bearer as `SecretStr`, which pydantic masks when
dumped. The Vault stores the real values (sealed), so encoding reveals them explicitly and only
here. Readers (the sync Runner) call `decode_session`.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from botai.core.browser import SessionState


def encode_session(state: SessionState) -> str:
    """JSON for the Vault. Contains secrets: it must go straight into `Vault.put`."""
    return json.dumps(
        {
            "origin": state.origin,
            "cookies": [
                {
                    "name": c.name,
                    "value": c.value.get_secret_value(),
                    "domain": c.domain,
                    "path": c.path,
                    "expires": c.expires,
                    "http_only": c.http_only,
                    "secure": c.secure,
                }
                for c in state.cookies
            ],
            "bearer": state.bearer.get_secret_value() if state.bearer else None,
            "captured_at": state.captured_at.isoformat(),
            "expires_hint": state.expires_hint.isoformat() if state.expires_hint else None,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_session(raw: str) -> SessionState:
    return SessionState.model_validate_json(raw)


def httpx_cookies(state: SessionState) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in state.cookies:
        jar.set(c.name, c.value.get_secret_value(), domain=c.domain.lstrip("."), path=c.path)
    return jar


def request_headers(state: SessionState) -> dict[str, str]:
    """Headers a sync request must carry besides cookies."""
    if state.bearer is None:
        return {}
    return {"authorization": f"Bearer {state.bearer.get_secret_value()}"}


def jwt_expiry(token: str) -> datetime | None:
    """`exp` of a JWT, read without verification. A hint for refresh planning, nothing more."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload: Any = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        exp = payload.get("exp") if isinstance(payload, dict) else None
        return datetime.fromtimestamp(float(exp), UTC) if exp is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None
