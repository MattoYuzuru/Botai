"""The worker as production starts it: services built from settings, a handler for every job kind.

Nothing here connects to a database or launches a browser; the point is that the factories of the
streams fit each other (the login flow needs the Playwright gateway, the sync clients need the
same egress as the browser) and that no job the API can enqueue is left without a handler.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from botai.browser import egress as egress_module
from botai.browser.gateway import PlaywrightGateway
from botai.browser.login import LoginFlow
from botai.core.jobs import JobKind
from botai.core.llm import ScriptedLlm
from botai.core.vault import generate_kek_b64
from botai.db.session import get_engine, get_sessionmaker
from botai.jobs.queue import PgJobQueue
from botai.settings import Settings
from botai.vault.pg import PgVault
from botai.wiring import build_services
from botai.worker import handler_table
from tests.e2e.support import PROXY_MASTER


@pytest.fixture
def clean_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`build_services` fills process-wide caches; a test must leave them as it found them."""
    monkeypatch.setenv("BOTAI_PROXY_SECRET", PROXY_MASTER)
    monkeypatch.setattr(egress_module, "_cached", None)
    yield
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()


def test_the_services_of_the_worker_are_built_from_settings(clean_process_state: None) -> None:
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        env="test",
        botai_kek=generate_kek_b64(),  # type: ignore[arg-type]
        egress_proxy_url="http://127.0.0.1:3128",
        llm_provider="mock",
    )
    services = build_services(settings)

    assert isinstance(services.browser, PlaywrightGateway)
    assert isinstance(services.sessions, LoginFlow)
    assert isinstance(services.vault, PgVault)
    assert isinstance(services.queue, PgJobQueue)
    assert isinstance(services.llm, ScriptedLlm)
    # the browser, the login flow and the sync clients obtain proxy tokens from one egress
    assert egress_module.get_egress(settings) is egress_module.get_egress(settings)


def test_every_kind_of_job_the_api_can_enqueue_has_a_handler() -> None:
    assert set(handler_table()) == set(JobKind)
