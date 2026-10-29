"""Optional private, encrypted research checkpoint capability. Never an API or model payload."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import SecretStr


class ResearchConflict(ValueError):
    """Checkpoint CAS or scope mismatch; does not itself revoke the job claim."""


class ResearchFormatError(ValueError):
    """Unsupported checkpoint storage format; never recover partial plaintext."""


@dataclass(frozen=True, slots=True)
class ResearchCheckpoint:
    revision: int
    intent_key: str
    payload: SecretStr = field(repr=False)


@runtime_checkable
class ResearchContext(Protocol):
    async def load_research(self) -> ResearchCheckpoint | None: ...

    async def save_research(
        self, payload: SecretStr, *, intent_key: str, expected_revision: int
    ) -> ResearchCheckpoint: ...
