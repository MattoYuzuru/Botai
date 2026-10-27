"""Owner: W2. Replace with PlaywrightGateway and the real SessionProvider — keep the signatures."""

from __future__ import annotations

from botai.core.browser import BrowserGateway, SessionProvider, SessionState
from botai.core.errors import not_implemented
from botai.core.vault import Vault
from botai.settings import Settings


class _NotReadyGateway:
    async def open(self, **_: object) -> object:
        raise not_implemented("browser gateway")


class _NotReadySessions:
    async def ensure(self, ctx: object, *, user_id: str, connection_id: str) -> SessionState:
        raise not_implemented("session provider")


def build_gateway(settings: Settings) -> BrowserGateway:
    return _NotReadyGateway()  # type: ignore[return-value]


def build_sessions(settings: Settings, gateway: BrowserGateway, vault: Vault) -> SessionProvider:
    return _NotReadySessions()
