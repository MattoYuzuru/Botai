"""Owner: W1. The job queue of the Services container."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.queue import JobQueue
from botai.jobs.queue import PgJobQueue
from botai.settings import Settings


def build_queue(settings: Settings, db: async_sessionmaker[AsyncSession]) -> JobQueue:
    return PgJobQueue(db)
