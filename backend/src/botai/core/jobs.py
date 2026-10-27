"""Job context protocol (implementation: botai.jobs.PgJobContext, W1) and an in-memory double.

Producers (login flow, explorer, repair, chat) talk to the user only through JobContext:
`status` for the stage line, `emit` for blocks, `ask` for blocks that need an answer,
`ask_secret` for `input` blocks with `secret=True` (the value is sealed in the Vault by the API
and handed to the worker; it never appears in job events, logs or LLM prompts).
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from pydantic import SecretStr

from botai.contracts.agent_ui import AgentUiAnswer, AgentUiBlock, InputBlock


class JobKind(StrEnum):
    CONNECT = "connect"  # login + learn for a new connection
    LEARN = "learn"
    SYNC = "sync"
    REPAIR = "repair"
    INGEST = "ingest"  # document source
    CHAT = "chat"
    ACTION = "action"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobCancelled(Exception):
    """Raised inside a handler when the user cancelled or the wait TTL expired."""


class JobContext(Protocol):
    job_id: str
    user_id: str
    connection_id: str | None

    async def status(self, stage: str, *, state: JobState = JobState.RUNNING) -> None: ...

    async def emit(self, block: AgentUiBlock) -> None:
        """Send a block to the client. Idempotent by block.id (re-emit replaces)."""
        ...

    async def ask(self, block: AgentUiBlock) -> AgentUiAnswer:
        """Emit an asking block and wait for the answer (job state → waiting_user)."""
        ...

    async def ask_secret(self, block: InputBlock) -> SecretStr:
        """Emit a secret input block, wait for the answer, consume it. Requires block.secret."""
        ...


Responder = Callable[[AgentUiBlock], "str | list[str] | bool | None"]


class InMemoryJobContext:
    """Test double. `responder(block)` returns the user's answer for asking blocks; for secret
    inputs it returns the secret string. No responder (or None answer) means the user never
    answers: the call raises JobCancelled, like a wait TTL in production."""

    def __init__(
        self,
        *,
        user_id: str = "01J0000000000000000000USER",
        job_id: str = "01J00000000000000000000JOB",
        connection_id: str | None = None,
        responder: Responder | None = None,
    ) -> None:
        self.job_id = job_id
        self.user_id = user_id
        self.connection_id = connection_id
        self._responder = responder
        self.statuses: list[tuple[str, JobState]] = []
        self.blocks: list[AgentUiBlock] = []
        self.secrets_requested: list[str] = []

    async def status(self, stage: str, *, state: JobState = JobState.RUNNING) -> None:
        self.statuses.append((stage, state))

    async def emit(self, block: AgentUiBlock) -> None:
        self.blocks = [b for b in self.blocks if b.id != block.id] + [block]

    def _respond(self, block: AgentUiBlock) -> str | list[str] | bool:
        value = self._responder(block) if self._responder else None
        if value is None:
            raise JobCancelled(f"user did not answer block {block.id} ({block.type})")
        return value

    async def ask(self, block: AgentUiBlock) -> AgentUiAnswer:
        if isinstance(block, InputBlock) and block.secret:
            raise ValueError("use ask_secret for secret inputs")
        await self.emit(block)
        return AgentUiAnswer(block_id=block.id, values=self._respond(block))

    async def ask_secret(self, block: InputBlock) -> SecretStr:
        if not block.secret:
            raise ValueError("ask_secret requires InputBlock(secret=True)")
        await self.emit(block)
        self.secrets_requested.append(block.id)
        value = self._respond(block)
        if not isinstance(value, str):
            raise TypeError("secret answer must be a string")
        return SecretStr(value)
