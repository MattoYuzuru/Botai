from __future__ import annotations

import pytest

from botai.core.vault import (
    InMemoryVault,
    SecretKind,
    VaultError,
    aad_for,
    generate_kek_b64,
    load_kek,
    seal,
    unseal,
)


async def test_roundtrip_and_no_plaintext_at_rest() -> None:
    vault = InMemoryVault()
    await vault.put("u1", "c1", SecretKind.PASSWORD, "P@ssw0rd-FIXTURE")
    got = await vault.get("u1", "c1", SecretKind.PASSWORD)
    assert got is not None
    assert got.get_secret_value() == "P@ssw0rd-FIXTURE"
    for sealed in vault.raw().values():
        assert b"P@ssw0rd-FIXTURE" not in sealed.ciphertext + sealed.wrapped_dek + sealed.nonce
    assert "P@ssw0rd" not in repr(got)


def test_aad_binds_user_and_connection() -> None:
    kek = load_kek(generate_kek_b64())
    sealed = seal(kek, "k1", aad_for("u1", "c1", SecretKind.SESSION, "k1"), b"cookie")
    with pytest.raises(VaultError):
        unseal(kek, aad_for("u2", "c1", SecretKind.SESSION, "k1"), sealed)
    with pytest.raises(VaultError):
        unseal(kek, aad_for("u1", "c2", SecretKind.SESSION, "k1"), sealed)
    with pytest.raises(VaultError):
        unseal(load_kek(generate_kek_b64()), aad_for("u1", "c1", SecretKind.SESSION, "k1"), sealed)
    assert unseal(kek, aad_for("u1", "c1", SecretKind.SESSION, "k1"), sealed) == b"cookie"


def test_kek_must_be_32_bytes() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        load_kek("c2hvcnQ=")
