"""PgVault (NFR-002, NFR-003): ciphertext only at rest, AAD binds user and connection, TTL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.clock import FrozenClock
from botai.core.vault import (
    CipherVault,
    SecretKind,
    TransactionalVault,
    VaultError,
    generate_kek_b64,
    load_kek,
)
from botai.db.models import SecretRow
from botai.settings import Settings
from botai.vault.factory import build_vault
from botai.vault.pg import PgVault
from tests.auth.support import make_user

pytestmark = pytest.mark.db

MARKER = "P@ssw0rd-FIXTURE"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

Sm = async_sessionmaker[AsyncSession]


def _vault(sm: Sm, clock: FrozenClock | None = None, kek: bytes | None = None) -> PgVault:
    return PgVault(
        sm,
        {"k1": kek or load_kek(generate_kek_b64())},
        "k1",
        clock=clock or FrozenClock(NOW),
    )


async def test_roundtrip_has_delete(tx_sessionmaker: Sm) -> None:
    user = await make_user(tx_sessionmaker)
    vault = _vault(tx_sessionmaker)
    assert await vault.get(user, "c1", SecretKind.PASSWORD) is None
    assert not await vault.has(user, "c1", SecretKind.PASSWORD)

    await vault.put(user, "c1", SecretKind.PASSWORD, MARKER)
    got = await vault.get(user, "c1", SecretKind.PASSWORD)
    assert got is not None
    assert got.get_secret_value() == MARKER
    assert MARKER not in repr(got)
    assert await vault.has(user, "c1", SecretKind.PASSWORD)
    assert not await vault.has(user, "c1", SecretKind.LOGIN)  # kinds are independent

    await vault.delete(user, "c1", SecretKind.PASSWORD)
    await vault.delete(user, "c1", SecretKind.PASSWORD)  # idempotent
    assert await vault.get(user, "c1", SecretKind.PASSWORD) is None


async def test_database_holds_no_plaintext(tx_sessionmaker: Sm) -> None:
    user = await make_user(tx_sessionmaker)
    vault = _vault(tx_sessionmaker)
    await vault.put(user, "c1", SecretKind.PASSWORD, MARKER)
    await vault.put(user, "c1", SecretKind.LOGIN, "student-login-FIXTURE")

    async with tx_sessionmaker() as session:
        rows = (await session.execute(select(SecretRow).where(SecretRow.user_id == user))).all()
        assert len(rows) == 2
        # whole-table dump, the way a leaked backup would look
        dump = (
            (
                await session.execute(
                    text("select secrets::text from secrets where user_id = :u"), {"u": user}
                )
            )
            .scalars()
            .all()
        )
    blob = b"".join(
        r.ciphertext + r.wrapped_dek + r.nonce
        for (r,) in rows  # type: ignore[misc]
    )
    for needle in (MARKER, "student-login-FIXTURE"):
        assert needle.encode() not in blob
        assert needle.encode().hex() not in "".join(dump)
        assert needle not in "".join(dump)
    # the KEK-wrapped DEK is per secret
    assert len({r.wrapped_dek for (r,) in rows}) == 2  # type: ignore[misc]


async def test_put_is_an_upsert_with_a_fresh_dek(tx_sessionmaker: Sm) -> None:
    user = await make_user(tx_sessionmaker)
    vault = _vault(tx_sessionmaker)
    await vault.put(user, "c1", SecretKind.TOKEN, "first")
    async with tx_sessionmaker() as s:
        first = (await s.execute(select(SecretRow).where(SecretRow.user_id == user))).scalar_one()
        first_dek, first_id = first.wrapped_dek, first.id
        assert first.rotated_at is None

    await vault.put(user, "c1", SecretKind.TOKEN, "second")
    async with tx_sessionmaker() as s:
        rows = (await s.execute(select(SecretRow).where(SecretRow.user_id == user))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].wrapped_dek != first_dek
    assert rows[0].rotated_at is not None
    got = await vault.get(user, "c1", SecretKind.TOKEN)
    assert got is not None
    assert got.get_secret_value() == "second"


async def test_row_moved_to_another_user_does_not_decrypt(tx_sessionmaker: Sm) -> None:
    alice = await make_user(tx_sessionmaker)
    bob = await make_user(tx_sessionmaker)
    vault = _vault(tx_sessionmaker)
    await vault.put(alice, "c1", SecretKind.PASSWORD, MARKER)

    async with tx_sessionmaker() as s:
        await s.execute(update(SecretRow).where(SecretRow.user_id == alice).values(user_id=bob))
        await s.commit()

    with pytest.raises(VaultError) as err:
        await vault.get(bob, "c1", SecretKind.PASSWORD)
    assert MARKER not in str(err.value)
    assert await vault.get(alice, "c1", SecretKind.PASSWORD) is None  # no longer hers


async def test_row_moved_to_another_connection_or_kind_does_not_decrypt(
    tx_sessionmaker: Sm,
) -> None:
    user = await make_user(tx_sessionmaker)
    vault = _vault(tx_sessionmaker)
    await vault.put(user, "c1", SecretKind.PASSWORD, MARKER)
    async with tx_sessionmaker() as s:
        await s.execute(
            update(SecretRow).where(SecretRow.user_id == user).values(connection_id="c2")
        )
        await s.commit()
    with pytest.raises(VaultError):
        await vault.get(user, "c2", SecretKind.PASSWORD)
    async with tx_sessionmaker() as s:
        await s.execute(update(SecretRow).where(SecretRow.user_id == user).values(kind="login"))
        await s.commit()
    with pytest.raises(VaultError):
        await vault.get(user, "c2", SecretKind.LOGIN)


async def test_a_user_cannot_read_anothers_secret(tx_sessionmaker: Sm) -> None:
    alice = await make_user(tx_sessionmaker)
    bob = await make_user(tx_sessionmaker)
    vault = _vault(tx_sessionmaker)
    await vault.put(alice, "c1", SecretKind.TOKEN, "alice-token")
    assert await vault.get(bob, "c1", SecretKind.TOKEN) is None
    assert not await vault.has(bob, "c1", SecretKind.TOKEN)
    await vault.delete(bob, "c1", SecretKind.TOKEN)  # must not touch Alice's row
    assert await vault.has(alice, "c1", SecretKind.TOKEN)


async def test_wrong_kek_fails_closed(tx_sessionmaker: Sm) -> None:
    user = await make_user(tx_sessionmaker)
    await _vault(tx_sessionmaker).put(user, "c1", SecretKind.PASSWORD, MARKER)
    with pytest.raises(VaultError):
        await _vault(tx_sessionmaker).get(user, "c1", SecretKind.PASSWORD)  # another random KEK


async def test_unknown_kek_id_fails_closed(tx_sessionmaker: Sm) -> None:
    user = await make_user(tx_sessionmaker)
    kek = load_kek(generate_kek_b64())
    await _vault(tx_sessionmaker, kek=kek).put(user, "c1", SecretKind.PASSWORD, MARKER)
    other = PgVault(tx_sessionmaker, {"k2": kek}, "k2")
    with pytest.raises(VaultError):
        await other.get(user, "c1", SecretKind.PASSWORD)


async def test_old_key_still_reads_after_rotation_of_current_key(tx_sessionmaker: Sm) -> None:
    user = await make_user(tx_sessionmaker)
    old, new = load_kek(generate_kek_b64()), load_kek(generate_kek_b64())
    await PgVault(tx_sessionmaker, {"k1": old}, "k1").put(user, "c1", SecretKind.TOKEN, "t")
    rotated = PgVault(tx_sessionmaker, {"k1": old, "k2": new}, "k2")
    got = await rotated.get(user, "c1", SecretKind.TOKEN)
    assert got is not None
    assert got.get_secret_value() == "t"
    await rotated.put(user, "c1", SecretKind.TOKEN, "t2")  # rewritten under the new key
    async with tx_sessionmaker() as s:
        assert (await s.execute(select(SecretRow.kek_id))).scalar_one() == "k2"


async def test_expired_secret_is_gone(tx_sessionmaker: Sm) -> None:
    user = await make_user(tx_sessionmaker)
    clock = FrozenClock(NOW)
    vault = _vault(tx_sessionmaker, clock)
    await vault.put(
        user, "job1", SecretKind.TRANSIENT, "123456", expires_at=NOW + timedelta(minutes=10)
    )
    await vault.put(user, "c1", SecretKind.PASSWORD, MARKER)  # no TTL

    clock.advance(timedelta(minutes=9))
    assert await vault.has(user, "job1", SecretKind.TRANSIENT)
    clock.advance(timedelta(minutes=2))
    assert not await vault.has(user, "job1", SecretKind.TRANSIENT)
    assert await vault.get(user, "job1", SecretKind.TRANSIENT) is None

    assert await vault.purge_expired() == 1
    async with tx_sessionmaker() as s:
        kinds = (
            (await s.execute(select(SecretRow.kind).where(SecretRow.user_id == user)))
            .scalars()
            .all()
        )
    assert kinds == ["password"]
    assert await vault.purge_expired() == 0


async def test_transient_without_ttl_gets_the_default_ten_minutes(tx_sessionmaker: Sm) -> None:
    user = await make_user(tx_sessionmaker)
    clock = FrozenClock(NOW)
    vault = _vault(tx_sessionmaker, clock)
    await vault.put(user, "job1", SecretKind.TRANSIENT, "hunter2-FIXTURE")
    async with tx_sessionmaker() as s:
        expires = (await s.execute(select(SecretRow.expires_at))).scalar_one()
    assert expires == NOW + timedelta(minutes=10)
    clock.advance(timedelta(minutes=10, seconds=1))
    assert await vault.get(user, "job1", SecretKind.TRANSIENT) is None


async def test_put_over_an_expired_secret_revives_the_slot(tx_sessionmaker: Sm) -> None:
    user = await make_user(tx_sessionmaker)
    clock = FrozenClock(NOW)
    vault = _vault(tx_sessionmaker, clock)
    await vault.put(user, "j", SecretKind.TRANSIENT, "old", expires_at=NOW + timedelta(minutes=1))
    clock.advance(timedelta(minutes=5))
    await vault.put(user, "j", SecretKind.TRANSIENT, "new")
    got = await vault.get(user, "j", SecretKind.TRANSIENT)
    assert got is not None
    assert got.get_secret_value() == "new"


async def test_transactional_put_commits_and_rolls_back_with_the_callers_write(
    tx_sessionmaker: Sm,
) -> None:
    user = await make_user(tx_sessionmaker)
    vault = _vault(tx_sessionmaker)
    assert isinstance(vault, TransactionalVault)
    await vault.put(user, "c1", SecretKind.SESSION, "before")
    async with tx_sessionmaker() as session, session.begin():
        await vault.put_in_transaction(session, user, "c1", SecretKind.SESSION, MARKER)
        row = await session.scalar(select(SecretRow).where(SecretRow.user_id == user))
        assert row is not None
        assert MARKER.encode() not in row.ciphertext
    got = await vault.get(user, "c1", SecretKind.SESSION)
    assert got is not None
    assert got.get_secret_value() == MARKER

    async def abort_publication() -> None:
        async with tx_sessionmaker() as session, session.begin():
            await session.execute(
                update(SecretRow).where(SecretRow.user_id == user).values(expires_at=NOW)
            )
            await vault.put_in_transaction(session, user, "c1", SecretKind.SESSION, "after")
            raise RuntimeError("abort guarded publication")

    with pytest.raises(RuntimeError, match="abort guarded publication"):
        await abort_publication()
    got = await vault.get(user, "c1", SecretKind.SESSION)
    assert got is not None
    assert got.get_secret_value() == MARKER


async def test_transactional_insert_does_not_escape_rollback_and_keeps_default_ttl(
    tx_sessionmaker: Sm,
) -> None:
    user = await make_user(tx_sessionmaker)
    vault = _vault(tx_sessionmaker)
    async with tx_sessionmaker() as session:
        await vault.put_in_transaction(session, user, "block", SecretKind.TRANSIENT, MARKER)
        row = await session.scalar(select(SecretRow).where(SecretRow.user_id == user))
        assert row is not None
        assert row.expires_at == NOW + timedelta(minutes=10)
        await session.rollback()
    assert await vault.get(user, "block", SecretKind.TRANSIENT) is None


async def test_pg_cipher_has_no_persistence_side_effect(tx_sessionmaker: Sm) -> None:
    vault = _vault(tx_sessionmaker)
    assert isinstance(vault, CipherVault)
    scope = "research:job:connection:v1:r1"
    sealed = vault.seal_value("user", scope, SecretKind.RESEARCH, MARKER)
    got = vault.open_value("user", scope, SecretKind.RESEARCH, sealed)
    assert got.get_secret_value() == MARKER
    assert MARKER not in repr(got)
    async with tx_sessionmaker() as session:
        assert not list(await session.scalars(select(SecretRow).where(SecretRow.user_id == "user")))
    with pytest.raises(VaultError):
        vault.open_value("user", scope.replace(":r1", ":r2"), SecretKind.RESEARCH, sealed)


def test_current_kek_must_be_loaded(tx_sessionmaker: Sm) -> None:
    with pytest.raises(ValueError, match="KEK id"):
        PgVault(tx_sessionmaker, {"k1": load_kek(generate_kek_b64())}, "k2")


def test_factory_builds_pg_vault_from_settings(tx_sessionmaker: Sm) -> None:
    settings = Settings(env="prod", botai_kek=generate_kek_b64(), botai_kek_id="k7")  # type: ignore[arg-type]
    assert isinstance(build_vault(settings, tx_sessionmaker), PgVault)


def test_factory_refuses_prod_without_kek(tx_sessionmaker: Sm) -> None:
    with pytest.raises(RuntimeError, match="BOTAI_KEK"):
        build_vault(Settings(env="prod", botai_kek=""), tx_sessionmaker)  # type: ignore[arg-type]


def test_factory_rejects_a_malformed_kek(tx_sessionmaker: Sm) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        build_vault(Settings(env="dev", botai_kek="c2hvcnQ="), tx_sessionmaker)  # type: ignore[arg-type]


async def test_dev_fallback_key_is_stable_across_processes(tx_sessionmaker: Sm) -> None:
    """api and worker are separate processes: both must derive the same dev key."""
    user = await make_user(tx_sessionmaker)
    dev = Settings(env="dev", botai_kek="")  # type: ignore[arg-type]
    await build_vault(dev, tx_sessionmaker).put(user, "c1", SecretKind.TOKEN, "t")
    got = await build_vault(dev, tx_sessionmaker).get(user, "c1", SecretKind.TOKEN)
    assert got is not None
    assert got.get_secret_value() == "t"
