"""Owner: W2. Builds the gateway and the session provider. Keep the signatures."""

from __future__ import annotations

from botai.core.browser import BrowserGateway, SessionProvider
from botai.core.vault import Vault
from botai.settings import Settings


def build_gateway(settings: Settings) -> BrowserGateway:
    # imported here so processes that never open a browser (the API) do not load Playwright
    from botai.browser.egress import get_egress
    from botai.browser.gateway import PlaywrightGateway

    return PlaywrightGateway(get_egress(settings))


def build_sessions(settings: Settings, gateway: BrowserGateway, vault: Vault) -> SessionProvider:
    from botai.browser.egress import get_egress
    from botai.browser.gateway import PlaywrightGateway
    from botai.browser.login import LoginFlow
    from botai.browser.presets import PgLoginStore
    from botai.db.session import get_sessionmaker

    if not isinstance(gateway, PlaywrightGateway):
        raise TypeError("LoginFlow drives the Playwright gateway")
    return LoginFlow(
        gateway=gateway,
        vault=vault,
        store=PgLoginStore(get_sessionmaker()),
        egress=get_egress(settings),
    )
