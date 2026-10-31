"""Small builders shared by the action tests."""

from __future__ import annotations

from typing import Any

from botai.actions.engine import ActionEngine
from botai.actions.models import ActionRequest

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
