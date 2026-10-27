"""Owner: W1. Builds the PgVault from settings. The KEK comes from the environment, never the DB."""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.vault import Vault, load_kek
from botai.settings import Settings
from botai.vault.pg import PgVault

log = logging.getLogger(__name__)


def _dev_kek() -> bytes:
    """Deterministic key for `dev`/`test` only, so api and worker processes agree without setup.

    It is public (derived from a constant): anything sealed with it is NOT protected. `prod`
    refuses to start without a real BOTAI_KEK.
    """
    return hashlib.sha256(b"botai-insecure-dev-kek").digest()


def build_vault(settings: Settings, db: async_sessionmaker[AsyncSession]) -> Vault:
    kek_b64 = settings.botai_kek.get_secret_value()
    if kek_b64:
        kek = load_kek(kek_b64)
    elif settings.env == "prod":
        raise RuntimeError("BOTAI_KEK is not set: refusing to start the Vault in prod")
    else:
        log.warning("BOTAI_KEK is not set: using the public development key (not secure)")
        kek = _dev_kek()
    return PgVault(db, {settings.botai_kek_id: kek}, settings.botai_kek_id)
