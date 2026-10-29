"""The `kind=session` Vault value: `SessionState` as JSON with the secrets revealed.

`SessionState` keeps cookie values and the bearer as `SecretStr`, which pydantic masks when
dumped. The Vault stores the real values (sealed), so encoding reveals them explicitly and only
here. Readers (the sync Runner) call `decode_session`.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from botai.core.browser import SessionOriginStorage, SessionState, auth_origin

MAX_STORAGE_ORIGINS = 12
MAX_STORAGE_ENTRIES = 64
MAX_STORAGE_KEY_CHARS = 256
MAX_STORAGE_VALUE_CHARS = 32_768
MAX_STORAGE_BYTES = 256 * 1024


@runtime_checkable
class ObservationSecretBrowser(Protocol):
    """Optional internal complete inventory; it is never a public session or model input."""

    async def observation_secrets(self) -> tuple[SecretStr, ...]: ...


@runtime_checkable
class EntrySessionProvider(Protocol):
    """Optional internal login capability; the private entry never becomes a shared preset."""

    async def ensure_for_entry(
        self,
        ctx: object,
        *,
        user_id: str,
        connection_id: str,
        entry_url: str,
        force: bool = False,
        manual: bool = False,
    ) -> SessionState: ...


def storage_origin(url: str, allow_hosts: Iterable[str]) -> str | None:
    """A plain HTTPS origin explicitly allowed for this connection, never a suffix grant."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if (
            parts.scheme != "https"
            or host not in {h.lower() for h in allow_hosts}
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
        ):
            return None
        return f"https://{host}"
    except ValueError:
        return None


def bounded_browser_storage(
    entries: Iterable[SessionOriginStorage], allow_hosts: Iterable[str]
) -> list[SessionOriginStorage]:
    """Enforce the same bounds and origin boundary on capture and on restoring Vault data."""
    hosts = tuple(allow_hosts)
    result: list[SessionOriginStorage] = []
    seen: set[str] = set()
    remaining = MAX_STORAGE_BYTES
    for entry in entries:
        origin = storage_origin(entry.origin, hosts)
        if origin is None or entry.origin.rstrip("/") != origin or origin in seen:
            continue
        if len(result) >= MAX_STORAGE_ORIGINS:
            break
        seen.add(origin)
        stores: list[dict[str, SecretStr]] = []
        for store in (entry.local_storage, entry.session_storage):
            kept: dict[str, SecretStr] = {}
            for key, value in store.items():
                if len(kept) >= MAX_STORAGE_ENTRIES:
                    break
                raw = value.get_secret_value()
                if (
                    not key
                    or len(key) > MAX_STORAGE_KEY_CHARS
                    or len(raw) > MAX_STORAGE_VALUE_CHARS
                ):
                    continue
                try:
                    size = len(key.encode("utf-8")) + len(raw.encode("utf-8"))
                except UnicodeError:
                    continue
                if size > remaining:
                    continue
                kept[key] = value
                remaining -= size
            stores.append(kept)
        if any(stores):
            result.append(
                SessionOriginStorage(
                    origin=origin, local_storage=stores[0], session_storage=stores[1]
                )
            )
    return result


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
            "entry_url": state.entry_url.get_secret_value() if state.entry_url else None,
            "bearer_scopes": [
                {
                    "origin": scope.origin,
                    "token": scope.token.get_secret_value(),
                    "captured_at": scope.captured_at.isoformat(),
                    "expires_hint": scope.expires_hint.isoformat() if scope.expires_hint else None,
                }
                for scope in state.bearer_scopes
            ],
            "captured_at": state.captured_at.isoformat(),
            "expires_hint": state.expires_hint.isoformat() if state.expires_hint else None,
            "browser_storage": [
                {
                    "origin": entry.origin,
                    "local_storage": {
                        key: value.get_secret_value() for key, value in entry.local_storage.items()
                    },
                    "session_storage": {
                        key: value.get_secret_value()
                        for key, value in entry.session_storage.items()
                    },
                }
                for entry in state.browser_storage
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_session(raw: str) -> SessionState:
    return SessionState.model_validate_json(raw)


def with_confirmed_entry(state: SessionState, entry_url: str) -> SessionState:
    """Called only after LoginFlow independently verified the owner's actual entry page."""
    return SessionState.model_validate({**state.model_dump(), "entry_url": SecretStr(entry_url)})


def session_storage_expiry(state: SessionState) -> datetime | None:
    """Private acquisition context outlives authentication; individual credentials still expire."""
    return None if state.entry_url is not None else state.expires_hint


def bearer_expired(
    token: SecretStr, *, now: datetime, expires_hint: datetime | None = None
) -> bool:
    """Explicit and JWT expiry hints each independently prohibit replaying a saved token."""
    expiry = jwt_expiry(token.get_secret_value())
    return (expires_hint is not None and expires_hint <= now) or (
        expiry is not None and expiry <= now
    )


def httpx_cookies(state: SessionState) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in state.cookies:
        jar.set(c.name, c.value.get_secret_value(), domain=c.domain.lstrip("."), path=c.path)
    return jar


def request_headers(state: SessionState, *, url: str | None = None) -> dict[str, str]:
    """Headers a sync request must carry besides cookies."""
    origin = auth_origin(url or state.origin)
    if origin is None:
        return {}
    for scope in state.bearer_scopes:
        if scope.origin == origin:
            return {"authorization": f"Bearer {scope.token.get_secret_value()}"}
    if state.bearer is None or auth_origin(state.origin) != origin:
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
