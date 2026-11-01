"""Source sessions for v4: the Vault handle convention, and a storage state a browser can apply.

`Source.auth_ref` is a handle, never a value: `<kind>:<scope>` where `<kind>` is one of the
`core.vault.SecretKind` values (`token`, `api_key`, `session`, `ical_url`, `password`, …) and
`<scope>` is the Vault scope the value was stored under. The bearer half is the agent wiring's
`VaultSecretReader` (one implementation for the whole product); this module adds the half a bearer
string cannot express:

* `session:<scope>` — a full Playwright storage state (cookies + localStorage) saved in the Vault;
* `profile:<name>` — the same state kept by a persistent browser profile (`BrowserProfileStore`).

Both become a mapping the runner hands to the browser factory and to its HTTP cookie jar. A source
whose handle names another kind (`token:…`) stays an HTTP source: returning None here is not an
"unauthenticated read", the bearer hook answers that case. A `session:`/`profile:` handle whose
state is missing raises: silently reading a site without the session would look like "no data".
Values are typed `SecretStr` until they are turned into the state mapping at this seam and are
never logged or included in a report.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr

from botai.browser.control import BrowserControl
from botai.browser.profiles import BrowserProfileStore
from botai.core.browser import auth_origin
from botai.core.errors import BotaiError, ErrorCode
from botai.core.vault import SecretKind, Vault
from botai.db.models import Source

SESSION_KIND_NAMES = frozenset({SecretKind.SESSION.value, "profile"})


async def capture_browser_session(control: BrowserControl) -> dict[str, Any]:
    """Storage plus browser-observed Bearer audiences; never infer tokens from storage keys."""
    state: dict[str, Any] = dict(await control.export_storage_state())
    headers: dict[str, dict[str, str]] = {}
    for entry in control.network_log():
        origin = auth_origin(entry.url)
        authorization = next(
            (
                value
                for key, value in entry.request_headers.items()
                if key.lower() == "authorization"
            ),
            "",
        )
        if origin and authorization.lower().startswith("bearer ") and authorization[7:].strip():
            headers[origin] = {"origin": origin, "authorization": authorization}
    state["http_headers"] = list(headers.values())
    return state


def _validation(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, detail)


def parse_auth_ref(auth_ref: str) -> tuple[str, str]:
    """Split a handle into `(kind, scope)`; a malformed handle is a loud configuration error."""
    kind_name, separator, scope = auth_ref.strip().partition(":")
    if not separator or not kind_name.strip() or not scope.strip():
        raise _validation(
            "auth_ref источника должен иметь вид «вид:область» (например, token:<id>)"
        )
    return kind_name.strip(), scope.strip()


class VaultSessionReader:
    """Resolve a source's `session:`/`profile:` handle to a Playwright storage state.

    The reader is a `SessionReader` for the runner. It returns None when the handle names a kind
    this reader does not own, so a token-authenticated source keeps working through the bearer
    hook and is not reported as "no session".
    """

    def __init__(self, vault: Vault, *, profiles: BrowserProfileStore | None = None) -> None:
        self._vault = vault
        self._profiles = profiles

    async def __call__(self, source: Source) -> Mapping[str, Any] | None:
        if not source.auth_ref:
            return None
        kind_name, scope = parse_auth_ref(source.auth_ref)
        if kind_name == "profile":
            return self._profile_state(source.user_id, scope)
        try:
            kind = SecretKind(kind_name)
        except ValueError:
            raise _validation(
                f"auth_ref источника: неизвестный вид секрета {kind_name!r}"
            ) from None
        if kind is not SecretKind.SESSION:
            return None  # token/api_key/password are the bearer/password hooks' business
        secret = await self._vault.get(source.user_id, scope, kind)
        if secret is None:
            raise _validation(
                "Сохранённой сессии источника нет в Vault (вид: session): источник не прочитан"
            )
        return self.parse_state(secret, where="Vault")

    def _profile_state(self, user_id: str, name: str) -> Mapping[str, Any]:
        profiles = self._profiles or BrowserProfileStore()
        state = profiles.load_storage_state(user_id, name)
        if state is None:
            raise _validation(
                f"У браузерного профиля {name!r} нет сохранённого состояния сессии: "
                "войдите в источник заново"
            )
        return dict(state)

    @staticmethod
    def parse_state(secret: SecretStr, *, where: str) -> dict[str, Any]:
        raw = secret.get_secret_value()
        try:
            state: Any = json.loads(raw)
        except ValueError:
            raise _validation(f"Состояние сессии в {where}: не JSON") from None
        if not isinstance(state, dict):
            raise _validation(f"Состояние сессии в {where}: ожидался объект storage state")
        return state


class AgentSessionStore:
    """Encrypted browser working state between runs, isolated by the authenticated user.

    Apply only this run's changes when saving: an unrelated origin added by another run is kept,
    and a cookie explicitly removed in this run is not resurrected from the previous snapshot.
    """

    SCOPE = "agent-browser-v4"

    def __init__(self, vault: Vault, *, user_id: str) -> None:
        self._vault = vault
        self._user_id = user_id
        self._initial: dict[str, Any] = {}

    async def load(self) -> dict[str, Any]:
        self._initial = await self._read()
        return self._initial

    async def _read(self) -> dict[str, Any]:
        secret = await self._vault.get(self._user_id, self.SCOPE, SecretKind.SESSION)
        return VaultSessionReader.parse_state(secret, where="Vault") if secret else {}

    async def save(self, state: Mapping[str, Any]) -> None:
        latest = await self._read()
        merged: dict[str, Any] = {}
        for name in ("cookies", "origins", "http_headers"):

            def keyed(raw: Mapping[str, Any], field: str = name) -> dict[tuple[str, ...], Any]:
                return {
                    (
                        (str(item.get("domain")), str(item.get("path")), str(item.get("name")))
                        if field == "cookies"
                        else (str(item.get("origin")),)
                    ): item
                    for item in raw.get(field, [])
                }

            before, after, current = keyed(self._initial), keyed(state), keyed(latest)
            for key in before.keys() - after.keys():
                if name != "http_headers" and current.get(key) == before[key]:
                    current.pop(key, None)
            for key, value in after.items():
                if before.get(key) != value:
                    current[key] = value
            merged[name] = list(current.values())
        await self._vault.put(
            self._user_id, self.SCOPE, SecretKind.SESSION, json.dumps(merged, ensure_ascii=False)
        )
        self._initial = dict(state)
