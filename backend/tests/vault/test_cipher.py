"""The optional cipher capability retains key custody and binds every external-storage scope."""

from __future__ import annotations

from dataclasses import replace

import pytest

from botai.core.vault import CipherVault, InMemoryVault, SecretKind, TransactionalVault, VaultError

MARKER = "private-research-marker-FIXTURE"
SCOPE = "research:job-1:connection-1:v1:r1:i" + "a" * 64


def test_memory_cipher_roundtrip_and_scope_isolation() -> None:
    vault = InMemoryVault()
    assert isinstance(vault, CipherVault)
    assert isinstance(vault, TransactionalVault)
    sealed = vault.seal_value("alice", SCOPE, SecretKind.RESEARCH, MARKER)
    opened = vault.open_value("alice", SCOPE, SecretKind.RESEARCH, sealed)
    assert opened.get_secret_value() == MARKER
    assert MARKER not in repr(opened)
    assert MARKER.encode() not in sealed.ciphertext + sealed.wrapped_dek + sealed.nonce
    assert vault.raw() == {}  # sealing does not persist an ordinary secret
    for user, scope, kind in (
        ("bob", SCOPE, SecretKind.RESEARCH),
        ("alice", SCOPE.replace("job-1", "job-2"), SecretKind.RESEARCH),
        ("alice", SCOPE.replace("connection-1", "connection-2"), SecretKind.RESEARCH),
        ("alice", SCOPE.replace(":v1:", ":v2:"), SecretKind.RESEARCH),
        ("alice", SCOPE.replace(":r1:", ":r2:"), SecretKind.RESEARCH),
        ("alice", SCOPE[:-1] + "b", SecretKind.RESEARCH),
        ("alice", SCOPE, SecretKind.SESSION),
    ):
        with pytest.raises(VaultError) as error:
            vault.open_value(user, scope, kind, sealed)
        assert MARKER not in str(error.value)


def test_cipher_rejects_unknown_versions_keys_and_corruption() -> None:
    vault = InMemoryVault()
    sealed = vault.seal_value("alice", SCOPE, SecretKind.RESEARCH, MARKER)
    for broken in (
        replace(sealed, aad_version=99),
        replace(sealed, kek_id="unknown"),
        replace(sealed, ciphertext=sealed.ciphertext[:-1]),
        replace(sealed, wrapped_dek=sealed.wrapped_dek[:-1]),
    ):
        with pytest.raises(VaultError):
            vault.open_value("alice", SCOPE, SecretKind.RESEARCH, broken)
