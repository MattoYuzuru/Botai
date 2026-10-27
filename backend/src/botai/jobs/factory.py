"""Owner: W1. Replace with PgJobQueue — keep the signature."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.queue import InMemoryJobQueue, JobQueue
from botai.settings import Settings


def build_queue(settings: Settings, db: async_sessionmaker[AsyncSession]) -> JobQueue:
    return InMemoryJobQueue()
