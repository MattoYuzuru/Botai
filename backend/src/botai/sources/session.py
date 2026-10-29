"""The user's session for a connection: read from the Vault, applied to outgoing requests.

The Vault holds `kind=session` as the JSON of `core.browser.SessionState` with the secret values
in clear inside the ciphertext (W2 seals it with `dump_session`). It exists in memory only for
the duration of one task and is never logged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from botai.core.browser import SessionCookie, SessionState, auth_origin
from botai.core.vault import SecretKind, Vault

log = logging.getLogger(__name__)


def recipe_requires_session(recipe: Mapping[str, Any]) -> bool:
    """Only effective request bindings may require a session for a scoped recipe."""
    auth = recipe.get("auth", {})
    if auth.get("type") != "session":
        return auth.get("type", "cookie") != "none"
    if any(endpoint["request"].get("render") for endpoint in recipe.get("endpoints", [])):
        return True  # Browser requests carry their own session, without HTTP binding references.
    referenced = {step.get("authBinding") for step in recipe.get("bootstrap", [])}
    referenced.update(
        endpoint["request"].get("authBinding")
        for endpoint in recipe.get("endpoints", [])
        if not endpoint["request"].get("render")
    )
    return any(
        binding.get("cookies") is True or binding.get("bearer") == "observedAuthorization"
        for name, binding in auth.get("bindings", {}).items()
        if name in referenced
    )


def dump_session(state: SessionState) -> str:
    """Serialise for the Vault, revealing the secret values inside the ciphertext."""
    data = state.model_dump(mode="json")
    for cookie, plain in zip(data["cookies"], state.cookies, strict=True):
        cookie["value"] = plain.value.get_secret_value()
    data["bearer"] = state.bearer.get_secret_value() if state.bearer else None
    data["entry_url"] = state.entry_url.get_secret_value() if state.entry_url else None
    for scope, plain in zip(data["bearer_scopes"], state.bearer_scopes, strict=True):
        scope["token"] = plain.token.get_secret_value()
    for origin, plain in zip(data["browser_storage"], state.browser_storage, strict=True):
        for name, store in (
            ("local_storage", plain.local_storage),
            ("session_storage", plain.session_storage),
        ):
            origin[name] = {key: value.get_secret_value() for key, value in store.items()}
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
    state: SessionState | None,
    *,
    auth_type: str,
    header_name: str | None,
    origin: str | None = None,
) -> dict[str, str]:
    if auth_type != "bearer" or state is None:
        return {}
    secret = scoped_bearer(state, origin or state.origin, allow_legacy=True)
    if secret is None:
        return {}
    token = secret.get_secret_value()
    if header_name and header_name.lower() != "authorization":
        return {header_name: token}
    return {"Authorization": f"Bearer {token}"}


def scoped_bearer(
    state: SessionState | None,
    origin: str,
    *,
    now: datetime | None = None,
    allow_legacy: bool = False,
) -> SecretStr | None:
    """A token's audience is an exact origin; old scalar tokens never prove a new binding."""
    target = auth_origin(origin)
    if state is None or target is None:
        return None
    for scope in state.bearer_scopes:
        if scope.origin == target:
            if now is not None and scope.expires_hint is not None and scope.expires_hint <= now:
                return None
            return scope.token
    if allow_legacy and auth_origin(state.origin) == target:
        return state.bearer
    return None
