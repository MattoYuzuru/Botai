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


@dataclass(frozen=True, slots=True)
class ResearchUsage:
    """Nonsecret cumulative work retained for the lifetime of the Job, outside knowledge TTL."""

    tokens: int = 0
    active_ms: int = 0
    requests: int = 0
    steps: int = 0

    def __post_init__(self) -> None:
        for value in (self.tokens, self.active_ms, self.requests, self.steps):
            if type(value) is not int or not 0 <= value <= 10**12:
                raise ValueError("invalid research usage")


@runtime_checkable
class ResearchBudgetContext(Protocol):
    async def load_research_usage(self) -> ResearchUsage | None: ...

    async def save_research_with_usage(
        self,
        payload: SecretStr,
        *,
        intent_key: str,
        expected_revision: int,
        usage: ResearchUsage,
    ) -> ResearchCheckpoint: ...
