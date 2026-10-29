"""PgVault: the Vault protocol over the `secrets` table (security.md §3, ADR-0001).

Every value is sealed with `core.vault.seal` (AES-256-GCM, a fresh DEK per secret, the DEK wrapped
by the KEK, AAD bound to user, connection and kind). The KEK never touches the database; the
table holds ciphertext only. Ordinary methods open short transactions. Guarded publishers use
`put_in_transaction` to commit or roll back the secret together with the caller's fenced write.
The optional pure cipher capability seals private external storage without persisting a secret.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.clock import Clock, SystemClock
from botai.core.ids import new_id
from botai.core.vault import (
    AAD_VERSION,
    SealedSecret,
    SecretKind,
    VaultError,
    aad_for,
    seal,
    unseal,
)
from botai.db.models import SecretRow

log = logging.getLogger(__name__)

TRANSIENT_TTL = timedelta(minutes=10)


class PgVault:
    def __init__(
        self,
        db: async_sessionmaker[AsyncSession],
        keks: Mapping[str, bytes],
        current_kek_id: str,
        *,
        clock: Clock | None = None,
        transient_ttl: timedelta = TRANSIENT_TTL,
    ) -> None:
        if current_kek_id not in keks:
            raise ValueError(f"current KEK id {current_kek_id!r} is not among the loaded keys")
        self._db = db
        self._keks = dict(keks)
        self._current = current_kek_id
        self._clock = clock or SystemClock()
        self._transient_ttl = transient_ttl

    def seal_value(self, user_id: str, scope_id: str, kind: SecretKind, value: str) -> SealedSecret:
        aad = aad_for(user_id, scope_id, kind, self._current)
        try:
            plaintext = value.encode()
        except UnicodeError:
            raise VaultError("secret is not valid UTF-8") from None
        return seal(self._keks[self._current], self._current, aad, plaintext)

    def open_value(
        self, user_id: str, scope_id: str, kind: SecretKind, sealed: SealedSecret
    ) -> SecretStr:
        if sealed.aad_version != AAD_VERSION:
            raise VaultError("secret uses an unsupported AAD version")
        kek = self._keks.get(sealed.kek_id)
        if kek is None:
            raise VaultError("secret was sealed with a key that is not loaded")
        aad = aad_for(user_id, scope_id, kind, sealed.kek_id, sealed.aad_version)
        try:
            return SecretStr(unseal(kek, aad, sealed).decode())
        except UnicodeError:
            raise VaultError("secret is not valid UTF-8") from None

    async def put(
        self,
        user_id: str,
        connection_id: str,
        kind: SecretKind,
        value: str,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        async with self._db() as session, session.begin():
            await self.put_in_transaction(
                session, user_id, connection_id, kind, value, expires_at=expires_at
            )

    async def put_in_transaction(
        self,
        session: AsyncSession,
        user_id: str,
        connection_id: str,
        kind: SecretKind,
        value: str,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        now = self._clock.now()
        if kind is SecretKind.TRANSIENT and expires_at is None:
            # A transient secret must never become immortal because a caller forgot the TTL.
            expires_at = now + self._transient_ttl
        sealed = self.seal_value(user_id, connection_id, kind, value)
        columns = {
            "kek_id": sealed.kek_id,
            "wrapped_dek": sealed.wrapped_dek,
            "nonce": sealed.nonce,
            "ciphertext": sealed.ciphertext,
            "aad_version": sealed.aad_version,
            "expires_at": expires_at,
        }
        stmt = pg_insert(SecretRow).values(
            id=new_id(),
            user_id=user_id,
            connection_id=connection_id,
            kind=kind.value,
            **columns,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SecretRow.user_id, SecretRow.connection_id, SecretRow.kind],
            set_={**columns, "rotated_at": now},
        )
        await session.execute(stmt)

    async def get(self, user_id: str, connection_id: str, kind: SecretKind) -> SecretStr | None:
        async with self._db() as session:
            row = await self._row(session, user_id, connection_id, kind)
        if row is None or self._expired(row.expires_at):
            return None
        sealed = SealedSecret(
            row.kek_id, row.wrapped_dek, row.nonce, row.ciphertext, row.aad_version
        )
        return self.open_value(user_id, connection_id, kind, sealed)

    async def delete(self, user_id: str, connection_id: str, kind: SecretKind) -> None:
        async with self._db() as session:
            await session.execute(
                delete(SecretRow).where(
                    SecretRow.user_id == user_id,
                    SecretRow.connection_id == connection_id,
                    SecretRow.kind == kind.value,
                )
            )
            await session.commit()

    async def has(self, user_id: str, connection_id: str, kind: SecretKind) -> bool:
        async with self._db() as session:
            row = await self._row(session, user_id, connection_id, kind)
        return row is not None and not self._expired(row.expires_at)

    async def purge_expired(self) -> int:
        """Physically removes expired rows (`get`/`has` already ignore them). Returns the count."""
        async with self._db() as session:
            result = await session.execute(
                delete(SecretRow).where(
                    SecretRow.expires_at.is_not(None), SecretRow.expires_at <= self._clock.now()
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def run_cleanup_forever(self, interval_s: float = 60.0) -> None:
        """Background task of the worker: expired transient secrets must not linger."""
        while True:
            try:
                removed = await self.purge_expired()
                if removed:
                    log.info("vault: purged %d expired secrets", removed)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("vault: cleanup failed")
            await asyncio.sleep(interval_s)

    def _expired(self, expires_at: datetime | None) -> bool:
        return expires_at is not None and expires_at <= self._clock.now()

    @staticmethod
    async def _row(
        session: AsyncSession, user_id: str, connection_id: str, kind: SecretKind
    ) -> SecretRow | None:
        result = await session.execute(
            select(SecretRow).where(
                SecretRow.user_id == user_id,
                SecretRow.connection_id == connection_id,
                SecretRow.kind == kind.value,
            )
        )
        return result.scalar_one_or_none()
