"""Owner: W1. Replace with PgVault (secrets table) — keep the signature."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.vault import InMemoryVault, Vault, load_kek
from botai.settings import Settings


def build_vault(settings: Settings, db: async_sessionmaker[AsyncSession]) -> Vault:
    kek = settings.botai_kek.get_secret_value()
    return InMemoryVault(load_kek(kek) if kek else None, settings.botai_kek_id)
