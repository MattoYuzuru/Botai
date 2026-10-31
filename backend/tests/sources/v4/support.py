"""Rows the v4 source tests need: a user, a space and the university behind it."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.core.ids import new_id
from botai.db.models import Job, Space, University, User


@dataclass(frozen=True, slots=True)
class Account:
    """A user with one university space."""

    user_id: str
    space_id: str
    university_id: str


async def create_account(
    sm: async_sessionmaker[AsyncSession], *, title: str = "Тестовый вуз"
) -> Account:
    """A user and their first space; committed to the test's shared transaction."""
    user_id = new_id()
    space_id = new_id()
    university_id = f"uni-{new_id().lower()}"
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
        session.add(
            University(
                id=university_id,
                title=title,
                short_title=title[:16],
                origins=[],
                accent_hue=262.2,
            )
        )
        await session.flush()
        session.add(Space(id=space_id, user_id=user_id, university_id=university_id, title=title))
        await session.commit()
    return Account(user_id=user_id, space_id=space_id, university_id=university_id)


async def add_space(
    sm: async_sessionmaker[AsyncSession], account: Account, *, title: str = "Второй вуз"
) -> str:
    """A second university space of the same user."""
    space_id = new_id()
    university_id = f"uni-{new_id().lower()}"
    async with sm() as session:
        session.add(
            University(
                id=university_id,
                title=title,
                short_title=title[:16],
                origins=[],
                accent_hue=200.0,
            )
        )
        await session.flush()
        session.add(
            Space(id=space_id, user_id=account.user_id, university_id=university_id, title=title)
        )
        await session.commit()
    return space_id


async def create_job(
    sm: async_sessionmaker[AsyncSession], user_id: str, *, kind: str = "chat"
) -> str:
    """A finished job row, so artifacts can point at a run that belongs to the user."""
    from datetime import UTC, datetime

    job_id = new_id()
    async with sm() as session:
        session.add(
            Job(
                id=job_id,
                user_id=user_id,
                kind=kind,
                state="done",
                payload={},
                run_after=datetime.now(UTC),
            )
        )
        await session.commit()
    return job_id
