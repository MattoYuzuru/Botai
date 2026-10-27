"""Owner: W2. Builds the gateway and the session provider. Keep the signatures."""

from __future__ import annotations

from botai.core.browser import BrowserGateway, SessionProvider, SessionState
from botai.core.errors import not_implemented
from botai.core.vault import Vault
from botai.settings import Settings


class _NotReadySessions:
    async def ensure(self, ctx: object, *, user_id: str, connection_id: str) -> SessionState:
        raise not_implemented("session provider")


def build_gateway(settings: Settings) -> BrowserGateway:
    # imported here so processes that never open a browser (the API) do not load Playwright
    from botai.browser.egress import get_egress
    from botai.browser.gateway import PlaywrightGateway

    return PlaywrightGateway(get_egress(settings))


def build_sessions(settings: Settings, gateway: BrowserGateway, vault: Vault) -> SessionProvider:
    return _NotReadySessions()
