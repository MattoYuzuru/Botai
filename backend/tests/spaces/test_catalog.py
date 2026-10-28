"""Suggestions do not imply a learned integration or create rows while searching."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select

from botai.core.clock import FrozenClock
from botai.db.models import University
from botai.spaces import catalog
from tests.auth.support import Sm, signup


def test_bundled_catalog_is_name_only_and_unambiguous() -> None:
    entries = catalog.entries()
    assert len(entries) == 20
    assert len({entry.id for entry in entries}) == len(entries)
    names: dict[str, str] = {}
    for entry in entries:
        for name in catalog.names(entry):
            key = catalog.name_key(name)
            assert names.setdefault(key, entry.id) == entry.id
        assert set(entry.model_dump()) == {"id", "title", "short_title", "aliases"}


@pytest.fixture
def suggested(monkeypatch: pytest.MonkeyPatch) -> catalog.CatalogEntry:
    entry = catalog.CatalogEntry(
        id="fakeuni",
        title="Учебный технологический университет",
        short_title="УТУ",
        aliases=("Учебный политех", "Fake University"),
    )
    monkeypatch.setattr(catalog, "entries", lambda: (entry,))
    return entry


async def test_alias_search_needs_no_learned_rows_and_does_not_persist(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    suggested: catalog.CatalogEntry,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    for query in ("у т у", "УЧЕБНЫЙ-ПОЛИТЕХ", "fake university", "технологический"):
        response = await client.get("/api/v1/universities", params={"q": query})
        assert response.status_code == 200
        assert [row["id"] for row in response.json()] == [suggested.id]
        assert response.json()[0]["origins"] == []
    async with tx_sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(University)) == 0


async def test_selection_persists_one_row_and_an_alias_reuses_it(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    suggested: catalog.CatalogEntry,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    first = await client.post(
        "/api/v1/spaces",
        json={
            "title": suggested.title,
            "universityId": suggested.id,
        },
    )
    assert first.status_code == 201
    second = await client.post("/api/v1/spaces", json={"title": suggested.aliases[0]})
    assert second.status_code == 201
    assert first.json()["universityId"] == second.json()["universityId"] == suggested.id
    async with tx_sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(University)) == 1


async def test_suggestion_preserves_an_existing_learned_id_and_metadata(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    suggested: catalog.CatalogEntry,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    async with tx_sessionmaker() as session:
        session.add(
            University(
                id="legacy-fakeuni",
                title=suggested.aliases[0],
                short_title="УП",
                origins=["https://lk.fakeuni.test"],
                accent_hue=300,
                accent_verified=True,
                third_party_warning="Нужна проверка человеком",
            )
        )
        await session.commit()
    result = (await client.get("/api/v1/universities", params={"q": "уту"})).json()
    assert len(result) == 1
    assert result[0]["id"] == "legacy-fakeuni"
    assert result[0]["thirdPartyWarning"] == "Нужна проверка человеком"
    response = await client.post(
        "/api/v1/spaces",
        json={
            "title": suggested.title,
            "universityId": suggested.id,
        },
    )
    assert response.status_code == 201
    assert response.json()["universityId"] == "legacy-fakeuni"


async def test_empty_query_keeps_all_learned_rows_for_space_labels(
    client: httpx.AsyncClient,
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    suggested: catalog.CatalogEntry,
) -> None:
    await signup(client, tx_sessionmaker, clock)
    async with tx_sessionmaker() as session:
        for index in range(25):
            session.add(
                University(
                    id=f"fixture-{index}",
                    title=f"Учебная академия {index}",
                    short_title=f"УА-{index}",
                    origins=[],
                    accent_hue=300,
                    accent_verified=False,
                )
            )
        await session.commit()
    result = (await client.get("/api/v1/universities")).json()
    assert len(result) == 26
    assert suggested.id in {row["id"] for row in result}
