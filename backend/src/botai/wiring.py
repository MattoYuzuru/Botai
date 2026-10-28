"""Builds the Services container. Each owner changes only their own factory module."""

from __future__ import annotations

from botai.agent.factory import build_llm
from botai.browser.factory import build_gateway, build_sessions
from botai.core.clock import SystemClock
from botai.core.embed import NoopEmbedder
from botai.core.services import Services
from botai.db.session import get_sessionmaker
from botai.jobs.factory import build_queue
from botai.search import SearchService
from botai.settings import Settings, get_settings
from botai.vault.factory import build_vault


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or get_settings()
    db = get_sessionmaker()
    vault = build_vault(settings, db)
    gateway = build_gateway(settings)
    return Services(
        settings=settings,
        db=db,
        clock=SystemClock(),
        vault=vault,
        llm=build_llm(settings),
        browser=gateway,
        sessions=build_sessions(settings, gateway, vault),
        queue=build_queue(settings, db),
        embedder=NoopEmbedder(),
        search=SearchService(settings, db=db),
    )
