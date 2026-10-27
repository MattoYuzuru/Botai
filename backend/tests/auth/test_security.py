from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from botai.auth import security


async def test_hash_is_argon2id_and_verifies() -> None:
    h = await security.hash_password("s3cret-FIXTURE")
    assert h.startswith("$argon2id$")
    assert "s3cret-FIXTURE" not in h
    assert await security.verify_password(h, "s3cret-FIXTURE")
    assert not await security.verify_password(h, "s3cret-fixture")


async def test_garbage_hash_is_a_mismatch_not_a_crash() -> None:
    assert not await security.verify_password("not-a-hash", "x")
    assert not await security.verify_password("x", "x")


async def test_same_password_hashes_differently() -> None:
    assert await security.hash_password("same-password") != await security.hash_password(
        "same-password"
    )


def test_weaker_parameters_trigger_a_rehash(monkeypatch: pytest.MonkeyPatch) -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("pw")
    assert not security.needs_rehash(weak)  # the (cheap) active hasher produced it
    strong = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    monkeypatch.setattr(security, "_hasher", strong)
    assert security.needs_rehash(weak)


def test_tokens_are_32_bytes_of_entropy_and_hashed_with_sha256() -> None:
    token = security.new_token()
    assert len(token) >= 43  # 32 bytes, base64url without padding
    assert security.new_token() != token
    digest = security.token_hash(token)
    assert len(digest) == 64
    assert token not in digest
    assert digest == security.token_hash(token)
