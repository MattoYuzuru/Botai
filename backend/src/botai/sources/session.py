"""The user's session for a connection: read from the Vault, applied to outgoing requests.

The Vault holds `kind=session` as the JSON of `core.browser.SessionState` with the secret values
in clear inside the ciphertext (W2 seals it with `dump_session`). It exists in memory only for
the duration of one task and is never logged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import httpx
from pydantic import ValidationError

from botai.core.browser import SessionCookie, SessionState
from botai.core.vault import SecretKind, Vault

log = logging.getLogger(__name__)


def dump_session(state: SessionState) -> str:
    """Serialise for the Vault, revealing the secret values inside the ciphertext."""
    data = state.model_dump(mode="json")
    for cookie, plain in zip(data["cookies"], state.cookies, strict=True):
        cookie["value"] = plain.value.get_secret_value()
    data["bearer"] = state.bearer.get_secret_value() if state.bearer else None
    return json.dumps(data, ensure_ascii=False)


async def load_session(vault: Vault, user_id: str, connection_id: str) -> SessionState | None:
    raw = await vault.get(user_id, connection_id, SecretKind.SESSION)
    if raw is None:
        return None
    try:
        return SessionState.model_validate_json(raw.get_secret_value())
    except ValidationError:
        log.warning("stored session for connection %s cannot be read", connection_id)
        return None


def known_expired(state: SessionState | None, now: datetime) -> bool:
    """A session whose own expiry hint has passed: no request is worth sending (T13)."""
    if state is None:
        return False
    return state.expires_hint is not None and state.expires_hint <= now


def _live(cookie: SessionCookie, now: datetime) -> bool:
    return cookie.expires is None or cookie.expires <= 0 or cookie.expires > now.timestamp()


def cookie_jar(state: SessionState | None, now: datetime) -> httpx.Cookies:
    jar = httpx.Cookies()
    if state is None:
        return jar
    for cookie in state.cookies:
        if _live(cookie, now):
            jar.set(
                cookie.name,
                cookie.value.get_secret_value(),
                domain=cookie.domain.lstrip("."),
                path=cookie.path or "/",
            )
    return jar


def auth_headers(
    state: SessionState | None, *, auth_type: str, header_name: str | None
) -> dict[str, str]:
    if auth_type != "bearer" or state is None or state.bearer is None:
        return {}
    token = state.bearer.get_secret_value()
    if header_name and header_name.lower() != "authorization":
        return {header_name: token}
    return {"Authorization": f"Bearer {token}"}
