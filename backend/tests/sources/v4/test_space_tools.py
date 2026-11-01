"""`space_create`: the v4 agent creates the student's university when the account has none.

The product path behind the «Вузов пока нет» -> «Подключить вуз» -> chat flow (03 §3.1/§3.2):
the UI has no create-university form, so the tool is the only way a chat turn can make a space and
then add sources to it. The test drives the real service (universities registry + one space per
university) over the test Postgres, with no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from botai.agent.v4.protocol import ToolContext, ToolHandler, ToolRegistry
from botai.agent.v4.tools.spaces import SpaceToolkit
from botai.core.ids import new_id
from botai.db.models import Space, University, User
from tests.auth.support import Sm

pytestmark = pytest.mark.db


async def _user_without_space(sm: Sm) -> str:
    """A registered student with no university: the exact production edge case."""
    user_id = new_id()
    async with sm() as session:
        session.add(
            User(
                id=user_id,
                email=f"{user_id.lower()}@fakeuni.test",
                display_name="Студент",
                password_hash="x",
                is_owner=False,
                disabled=False,
            )
        )
        await session.commit()
    return user_id


def _tools(sm: Sm) -> dict[str, ToolHandler]:
    toolkit = SpaceToolkit(sm=sm)
    registry = ToolRegistry(toolkit.tools())
    assert [tool.spec.name for tool in toolkit.tools()] == ["space_create"]
    assert registry.validate("space_create", {"title": "Тестовый университет"}) == []
    # `title` is required: the model must name the university it creates.
    assert registry.validate("space_create", {})
    return {tool.spec.name: tool for tool in toolkit.tools()}


def _context(user_id: str, workspace: Path) -> ToolContext:
    return ToolContext(
        run_id="run-space-1", task="подключи вуз", user_id=user_id, workspace=workspace
    )


async def test_creates_the_first_university_and_its_space(
    tx_sessionmaker: Sm, tmp_path: Path
) -> None:
    user_id = await _user_without_space(tx_sessionmaker)
    tools = _tools(tx_sessionmaker)

    result = await tools["space_create"].run(
        {"title": "Тестовый университет", "homepage_url": "https://uni.example.org"},
        _context(user_id, tmp_path),
    )

    assert result.ok, result.error
    space = result.data["space"]
    assert space["created"] is True
    assert space["title"] == "Тестовый университет"
    assert space["universityId"]
    # The returned space_id is exactly what `source_create` needs next.
    async with tx_sessionmaker() as session:
        rows = list(await session.scalars(select(Space).where(Space.user_id == user_id)))
        assert [row.id for row in rows] == [space["id"]]
        university = await session.get(University, space["universityId"])
        assert university is not None
        assert university.short_title
        # The site the student named became a registry origin, not a source address.
        assert "https://uni.example.org" in university.origins


async def test_the_same_university_is_never_created_twice(
    tx_sessionmaker: Sm, tmp_path: Path
) -> None:
    user_id = await _user_without_space(tx_sessionmaker)
    tools = _tools(tx_sessionmaker)
    context = _context(user_id, tmp_path)

    first = await tools["space_create"].run({"title": "Тестовый университет"}, context)
    second = await tools["space_create"].run({"title": "Тестовый университет"}, context)

    assert first.ok
    assert second.ok
    assert second.data["space"]["created"] is False
    assert second.data["space"]["id"] == first.data["space"]["id"]
    async with tx_sessionmaker() as session:
        count = await session.scalar(
            select(func.count()).select_from(Space).where(Space.user_id == user_id)
        )
    assert count == 1
    # The catalogue row (a university is shared between the two accounts) stays one as well.
    async with tx_sessionmaker() as session:
        universities = await session.scalar(
            select(func.count())
            .select_from(University)
            .where(University.id == first.data["space"]["universityId"])
        )
    assert universities == 1


async def test_a_site_address_instead_of_a_name_is_a_validation_error(
    tx_sessionmaker: Sm, tmp_path: Path
) -> None:
    user_id = await _user_without_space(tx_sessionmaker)
    tools = _tools(tx_sessionmaker)

    result = await tools["space_create"].run(
        {"title": "https://lk.example.org/cabinet"}, _context(user_id, tmp_path)
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "validation"
    assert result.error.detail
    async with tx_sessionmaker() as session:
        count = await session.scalar(
            select(func.count()).select_from(Space).where(Space.user_id == user_id)
        )
    assert count == 0


async def test_an_unknown_catalogue_id_is_reported_not_turned_into_a_new_university(
    tx_sessionmaker: Sm, tmp_path: Path
) -> None:
    user_id = await _user_without_space(tx_sessionmaker)
    tools = _tools(tx_sessionmaker)

    result = await tools["space_create"].run(
        {"title": "Тестовый университет", "university_id": "no-such-university"},
        _context(user_id, tmp_path),
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "not_found"
    async with tx_sessionmaker() as session:
        count = await session.scalar(
            select(func.count()).select_from(Space).where(Space.user_id == user_id)
        )
    assert count == 0


async def test_a_tool_call_without_a_user_is_refused(tx_sessionmaker: Sm, tmp_path: Path) -> None:
    tools = _tools(tx_sessionmaker)
    context = ToolContext(run_id="run-space-2", task="", user_id=None, workspace=tmp_path)

    result = await tools["space_create"].run({"title": "Тестовый университет"}, context)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "no_user"
