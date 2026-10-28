"""Service container passed to job handlers. Built once per process by botai.wiring."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.browser import BrowserGateway, SessionProvider
from botai.core.clock import Clock
from botai.core.embed import Embedder
from botai.core.llm import LlmProvider
from botai.core.queue import JobQueue
from botai.core.vault import Vault
from botai.search import SearchService
from botai.settings import Settings


@dataclass(slots=True)
class Services:
    settings: Settings
    db: async_sessionmaker[AsyncSession]
    clock: Clock
    vault: Vault
    llm: LlmProvider
    browser: BrowserGateway
    sessions: SessionProvider
    queue: JobQueue
    embedder: Embedder
    search: SearchService | None = None
