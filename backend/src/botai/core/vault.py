"""Envelope encryption for connection secrets (security.md §3).

AES-256-GCM. A fresh data key (DEK) per secret, wrapped by the master key (KEK) which lives
outside the database. AAD binds the ciphertext to user, connection and kind: a row moved to
another user or connection does not decrypt.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

AAD_VERSION = 1


class SecretKind(StrEnum):
    PASSWORD = "password"  # noqa: S105 - enum label, not a secret
    LOGIN = "login"
    SESSION = "session"
    TOKEN = "token"  # noqa: S105
    ICAL_URL = "ical_url"
    API_KEY = "api_key"
    LLM_KEY = "llm_key"
    TRANSIENT = "transient"  # secret answers to agent blocks, TTL 10 min


@dataclass(frozen=True, slots=True)
class SealedSecret:
    kek_id: str
    wrapped_dek: bytes  # nonce(12) || AESGCM(KEK, DEK)
    nonce: bytes
    ciphertext: bytes  # includes GCM tag
    aad_version: int


class VaultError(Exception):
    """Decryption failed: wrong key, wrong AAD or tampered data. Never includes plaintext."""


def load_kek(b64: str) -> bytes:
    key = base64.b64decode(b64.encode(), validate=True)
    if len(key) != 32:
        raise ValueError("BOTAI_KEK must be base64 of exactly 32 bytes")
    return key


def generate_kek_b64() -> str:
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()


def aad_for(
    user_id: str, connection_id: str, kind: SecretKind, kek_id: str, version: int = AAD_VERSION
) -> bytes:
    return f"v{version}|{user_id}|{connection_id}|{kind.value}|{kek_id}".encode()


def seal(kek: bytes, kek_id: str, aad: bytes, plaintext: bytes) -> SealedSecret:
    dek = AESGCM.generate_key(bit_length=256)
    wrap_nonce = os.urandom(12)
    wrapped = wrap_nonce + AESGCM(kek).encrypt(wrap_nonce, dek, aad)
    nonce = os.urandom(12)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
    return SealedSecret(kek_id, wrapped, nonce, ciphertext, AAD_VERSION)


def unseal(kek: bytes, aad: bytes, sealed: SealedSecret) -> bytes:
    try:
        wrap_nonce, wrapped = sealed.wrapped_dek[:12], sealed.wrapped_dek[12:]
        dek = AESGCM(kek).decrypt(wrap_nonce, wrapped, aad)
        return AESGCM(dek).decrypt(sealed.nonce, sealed.ciphertext, aad)
    except (InvalidTag, ValueError) as exc:
        raise VaultError("secret cannot be decrypted with this key and context") from exc


class Vault(Protocol):
    async def put(
        self,
        user_id: str,
        connection_id: str,
        kind: SecretKind,
        value: str,
        *,
        expires_at: datetime | None = None,
    ) -> None: ...

    async def get(self, user_id: str, connection_id: str, kind: SecretKind) -> SecretStr | None: ...

    async def delete(self, user_id: str, connection_id: str, kind: SecretKind) -> None: ...

    async def has(self, user_id: str, connection_id: str, kind: SecretKind) -> bool: ...


class InMemoryVault:
    """Test implementation with the same crypto as the real one."""

    def __init__(self, kek: bytes | None = None, kek_id: str = "test") -> None:
        self._kek = kek or AESGCM.generate_key(bit_length=256)
        self._kek_id = kek_id
        self._rows: dict[tuple[str, str, SecretKind], SealedSecret] = {}

    async def put(
        self,
        user_id: str,
        connection_id: str,
        kind: SecretKind,
        value: str,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        aad = aad_for(user_id, connection_id, kind, self._kek_id)
        self._rows[(user_id, connection_id, kind)] = seal(
            self._kek, self._kek_id, aad, value.encode()
        )

    async def get(self, user_id: str, connection_id: str, kind: SecretKind) -> SecretStr | None:
        sealed = self._rows.get((user_id, connection_id, kind))
        if sealed is None:
            return None
        aad = aad_for(user_id, connection_id, kind, sealed.kek_id, sealed.aad_version)
        return SecretStr(unseal(self._kek, aad, sealed).decode())

    async def delete(self, user_id: str, connection_id: str, kind: SecretKind) -> None:
        self._rows.pop((user_id, connection_id, kind), None)

    async def has(self, user_id: str, connection_id: str, kind: SecretKind) -> bool:
        return (user_id, connection_id, kind) in self._rows

    def raw(self) -> dict[tuple[str, str, SecretKind], SealedSecret]:
        """For tests that check no plaintext is stored."""
        return dict(self._rows)
