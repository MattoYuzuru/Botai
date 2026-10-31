"""Artifacts of a source and the mutable cursor/site-map state of reading it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from botai.core.errors import BotaiError, ErrorCode
from botai.sources.v4 import artifacts, service, state
from tests.auth.support import Sm
from tests.sources.v4.support import Account, create_job

pytestmark = pytest.mark.db

SHA = "c" * 64


async def make_source(sm: Sm, account: Account, *, url: str = "https://lk.example.org/schedule"):
    async with sm() as session:
        source = await service.create_source(
            session,
            user_id=account.user_id,
            space_id=account.space_id,
            title="Личный кабинет",
            entry_url=url,
        )
        await session.commit()
        return source


async def test_an_artifact_is_recorded_with_content_address_and_provenance(
    tx_sessionmaker: Sm, account: Account
) -> None:
    source = await make_source(tx_sessionmaker, account)
    captured = datetime(2026, 10, 31, 9, 30, tzinfo=UTC)
    async with tx_sessionmaker() as session:
        run = await create_job(tx_sessionmaker, account.user_id)
        row = await artifacts.add_artifact(
            session,
            user_id=account.user_id,
            source_id=source.id,
            kind="page",
            sha256=SHA.upper(),
            url="https://lk.example.org/schedule?week=1",
            content_type="text/html",
            size=2048,
            storage_ref="sha256/" + SHA,
            metadata={"status": 200, "title": "Расписание"},
            captured_at=captured,
            run_id=run,
            step=2,
        )
        await session.commit()
    assert row.kind == "page"
    assert row.sha256 == SHA
    assert row.user_id == account.user_id
    assert row.source_id == source.id
    assert row.run_id == run
    assert row.step == 2
    assert row.metadata_json == {"status": 200, "title": "Расписание"}
    assert row.captured_at == captured
    assert row.storage_ref == "sha256/" + SHA
    async with tx_sessionmaker() as session:
        rows = await artifacts.list_artifacts(session, user_id=account.user_id, source_id=source.id)
    assert [item.id for item in rows] == [row.id]


async def test_artifact_kind_filter_and_validation(tx_sessionmaker: Sm, account: Account) -> None:
    source = await make_source(tx_sessionmaker, account)
    async with tx_sessionmaker() as session:
        await artifacts.add_artifact(
            session, user_id=account.user_id, source_id=source.id, kind="page", sha256=SHA
        )
        await artifacts.add_artifact(
            session,
            user_id=account.user_id,
            source_id=source.id,
            kind="har",
            sha256="d" * 64,
        )
        await session.commit()
    async with tx_sessionmaker() as session:
        pages = await artifacts.list_artifacts(
            session, user_id=account.user_id, source_id=source.id, kind="page"
        )
    assert [row.kind for row in pages] == ["page"]
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await artifacts.add_artifact(
                session, user_id=account.user_id, source_id=source.id, kind="video", sha256=SHA
            )
    assert exc.value.code is ErrorCode.VALIDATION
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await artifacts.add_artifact(
                session, user_id=account.user_id, source_id=source.id, kind="page", sha256="nope"
            )
    assert exc.value.code is ErrorCode.VALIDATION
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await artifacts.list_artifacts(
                session, user_id=account.user_id, source_id=source.id, limit=0
            )
    assert exc.value.code is ErrorCode.VALIDATION


async def test_artifacts_are_scoped_to_the_owner_and_their_runs(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    source = await make_source(tx_sessionmaker, account)
    foreign_source = await make_source(
        tx_sessionmaker, second_account, url="https://other.example.org/schedule"
    )
    foreign_job = await create_job(tx_sessionmaker, second_account.user_id)
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await artifacts.add_artifact(
                session,
                user_id=account.user_id,
                source_id=foreign_source.id,
                kind="page",
                sha256=SHA,
            )
    assert exc.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await artifacts.add_artifact(
                session,
                user_id=account.user_id,
                source_id=source.id,
                kind="page",
                sha256=SHA,
                run_id=foreign_job,
            )
    assert exc.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await artifacts.list_artifacts(
                session, user_id=second_account.user_id, source_id=source.id
            )
    assert exc.value.code is ErrorCode.NOT_FOUND


async def test_state_is_created_then_updated_field_by_field(
    tx_sessionmaker: Sm, account: Account
) -> None:
    source = await make_source(tx_sessionmaker, account)
    async with tx_sessionmaker() as session:
        assert (
            await state.get_source_state(session, user_id=account.user_id, source_id=source.id)
            is None
        )
        row = await state.upsert_source_state(
            session,
            user_id=account.user_id,
            source_id=source.id,
            last_revision=3,
            cursor={"page": 2},
            site_map={"routes": ["/schedule", "/tasks"]},
            endpoints=[{"url": "/api/schedule", "method": "GET"}],
        )
        await session.commit()
    assert row.last_revision == 3
    assert row.cursor == {"page": 2}
    assert row.site_map == {"routes": ["/schedule", "/tasks"]}
    assert row.endpoints == [{"url": "/api/schedule", "method": "GET"}]
    async with tx_sessionmaker() as session:
        updated = await state.upsert_source_state(
            session, user_id=account.user_id, source_id=source.id, cursor={"page": 3}
        )
        await session.commit()
    assert updated.cursor == {"page": 3}
    assert updated.last_revision == 3
    assert updated.site_map == {"routes": ["/schedule", "/tasks"]}
    async with tx_sessionmaker() as session:
        cleared = await state.upsert_source_state(
            session, user_id=account.user_id, source_id=source.id, cursor={}, endpoints=[]
        )
        await session.commit()
    assert cleared.cursor == {}
    assert cleared.endpoints == []


async def test_state_is_scoped_and_validates_shapes(
    tx_sessionmaker: Sm, account: Account, second_account: Account
) -> None:
    source = await make_source(tx_sessionmaker, account)
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await state.upsert_source_state(
                session,
                user_id=second_account.user_id,
                source_id=source.id,
                cursor={"page": 1},
            )
    assert exc.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await state.upsert_source_state(
                session,
                user_id=account.user_id,
                source_id=source.id,
                endpoints=[object()],  # type: ignore[list-item]
            )
    assert exc.value.code is ErrorCode.VALIDATION
    with pytest.raises(BotaiError) as exc:
        async with tx_sessionmaker() as session:
            await state.upsert_source_state(
                session, user_id=account.user_id, source_id=source.id, last_revision=-1
            )
    assert exc.value.code is ErrorCode.VALIDATION
