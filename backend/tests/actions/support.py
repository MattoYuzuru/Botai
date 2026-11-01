"""Small builders shared by the action tests."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.actions.engine import ActionEngine
from botai.actions.models import ActionRequest
from botai.core.ids import new_id

ORIGIN = "https://fakeuni.test"
WRITE_URL = f"{ORIGIN}/deep-write"
RUN_ID = "01M68Z000000000000000000A1"


def write_request(**overrides: Any) -> ActionRequest:
    """A concrete POST to the synthetic university: a request for a study certificate."""
    payload: dict[str, Any] = {
        "transport": "http",
        "intent": "Подать заявку на справку об обучении",
        "method": "POST",
        "url": WRITE_URL,
        "body": {"language": "ru", "student_id": "S-12345"},
        "body_kind": "form",
    }
    payload.update(overrides)
    return ActionRequest.model_validate(payload)


async def add_v4_source(
    sm: async_sessionmaker[AsyncSession],
    *,
    user_id: str,
    space_id: str,
    title: str,
    status: str = "active",
) -> str:
    """A v4 source row for the journal-title tests (the real `sources` table, one space).

    Inserted with plain SQL over the stable columns: the tests do not depend on every column the
    source model grows over time (migrations of other lanes may be in flight in a shared tree).
    """
    source_id = new_id()
    async with sm() as session:
        await session.execute(
            text(
                "INSERT INTO sources (id, user_id, space_id, title, identity_key, status) "
                "VALUES (:id, :user_id, :space_id, :title, :identity_key, :status)"
            ),
            {
                "id": source_id,
                "user_id": user_id,
                "space_id": space_id,
                "title": title,
                "identity_key": f"test:{source_id}",
                "status": status,
            },
        )
        await session.commit()
    return source_id


async def allow(
    engine: ActionEngine,
    user_id: str,
    *,
    action_class: str = "write_soft",
    mode: str = "auto",
    limits: dict[str, int] | None = None,
) -> None:
    await engine.upsert_policy(
        user_id=user_id,
        scope_kind="global",
        scope_id=None,
        action_class=action_class,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        limits=limits,
    )
