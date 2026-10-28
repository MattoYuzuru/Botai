"""Optional, non-secret conversation capability of a learning job.

Credentials continue to use JobContext.ask/ask_secret. A dialogue turn is the student's own
plain-text clarification, never a native answer, browser command or approval of an external write.
Consumers use isinstance(ctx, DialogueContext) and retain their legacy JobContext fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from botai.contracts.agent_ui import InputBlock


@dataclass(frozen=True, slots=True)
class DialogueTurn:
    message_id: str
    text: str
    created_at: datetime
    reply_to_block_id: str | None = None


@runtime_checkable
class DialogueContext(Protocol):
    async def ask_dialogue(self, block: InputBlock) -> str:
        """Ask a non-secret learning question; accept a structured answer or targeted chat turn."""
        ...

    async def drain_dialogue(self) -> list[DialogueTurn]:
        """Ordered, unacknowledged turns for this job; replay until checkpoint_learning commits."""
        ...

    async def checkpoint_learning(self, intent: dict[str, Any]) -> None:
        """Atomically persist validated intent and acknowledge only the turns delivered so far."""
        ...
