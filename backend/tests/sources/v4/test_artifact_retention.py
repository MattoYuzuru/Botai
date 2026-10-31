"""The content-addressed artifact store and its retention: bytes never outlive their citations."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from botai.sources.v4 import artifacts, service, state
from botai.sources.v4.artifacts import FileArtifactStore
from tests.auth.support import Sm
from tests.sources.v4.support import Account

pytestmark = pytest.mark.db


def _present(storage_ref: str) -> bool:
    return Path(storage_ref).is_file()


def _absent(storage_ref: str) -> bool:
    return not Path(storage_ref).exists()


async def make_source(sm: Sm, account: Account) -> str:
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title="Хранилище",
            entry_url="https://lk.example.org/page",
        )
        await session.commit()
        return source.id


def test_store_addresses_bytes_by_content_and_is_stable(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    first = store.put(b"<html>page</html>")
    second = store.put(b"<html>page</html>")
    other = store.put(b'{"items": []}')
    assert first.sha256 == hashlib.sha256(b"<html>page</html>").hexdigest()
    assert first.storage_ref == second.storage_ref  # one file per content address
    assert first.storage_ref != other.storage_ref
    assert Path(first.storage_ref).is_file()
    assert store.get(first.storage_ref) == b"<html>page</html>"
    assert store.get(first.sha256) == b"<html>page</html>"  # a relative content address works
    assert store.refs() == sorted([first.storage_ref, other.storage_ref])


async def test_retention_keeps_cited_blobs_and_removes_the_rest(
    tx_sessionmaker: Sm, account: Account, tmp_path: Path
) -> None:
    source_id = await make_source(tx_sessionmaker, account)
    store = FileArtifactStore(tmp_path / "artifacts")
    cited = store.put(b"cited page")
    orphan = store.put(b"nobody points here")
    cited_by_state = store.put(b'{"kept": "by state"}')
    fresh = store.put(b"written a second ago, not yet cited")
    async with tx_sessionmaker() as session:
        await artifacts.add_artifact(
            session,
            user_id=account.user_id,
            source_id=source_id,
            kind="page",
            sha256=cited.sha256,
            storage_ref=cited.storage_ref,
        )
        # `source_state` may cite an artifact by its content address (a cursor, site map, endpoint).
        await state.upsert_source_state(
            session,
            user_id=account.user_id,
            source_id=source_id,
            cursor={"sample_sha256": cited_by_state.sha256},
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        removed = await artifacts.cleanup_unreferenced(session, store, min_age_s=3600)
    # The fresh orphan is younger than the protection window: retention never races a live run.
    assert removed == 0
    assert _present(orphan.storage_ref)
    async with tx_sessionmaker() as session:
        removed = await artifacts.cleanup_unreferenced(session, store)
    assert removed == 2  # orphan + fresh orphan; cited ones stay
    assert _present(cited.storage_ref)
    assert _present(cited_by_state.storage_ref)
    assert _absent(orphan.storage_ref)
    assert _absent(fresh.storage_ref)
    # The shard directory is gone with its last blob.
    assert _absent(str(Path(orphan.storage_ref).parent))


async def test_retention_can_keep_an_explicit_reference(
    tx_sessionmaker: Sm, account: Account, tmp_path: Path
) -> None:
    source_id = await make_source(tx_sessionmaker, account)
    store = FileArtifactStore(tmp_path / "artifacts")
    blob = store.put(b"referenced by an in-flight run")
    async with tx_sessionmaker() as session:
        await artifacts.add_artifact(
            session, user_id=account.user_id, source_id=source_id, kind="json", sha256="d" * 64
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        removed = await artifacts.cleanup_unreferenced(session, store, keep=[blob.storage_ref])
    assert removed == 0
    assert _present(blob.storage_ref)


async def test_referenced_refs_include_artifact_rows_and_state(
    tx_sessionmaker: Sm, account: Account
) -> None:
    source_id = await make_source(tx_sessionmaker, account)
    citation = "a" * 64
    async with tx_sessionmaker() as session:
        await artifacts.add_artifact(
            session,
            user_id=account.user_id,
            source_id=source_id,
            kind="page",
            sha256=citation,
            storage_ref=f"relative/{citation}",
        )
        await state.upsert_source_state(
            session,
            user_id=account.user_id,
            source_id=source_id,
            site_map={"last": "b" * 64},
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        refs = await artifacts.referenced_storage_refs(session)
    assert citation in refs
    assert f"relative/{citation}" in refs
    assert "b" * 64 in refs
