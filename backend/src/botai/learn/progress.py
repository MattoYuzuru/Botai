"""Private research ledger: portable proposals, bounded work and no browser/credential state."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from botai.contracts.specs import Kind
from botai.core.diagnostics import diagnostic_event
from botai.core.errors import BotaiError, ErrorCode
from botai.core.jobs import JobClaimLost, JobDeferred, JobInputChanged
from botai.core.llm import LlmProvider, LlmRequest, LlmResponse
from botai.core.research import (
    ResearchBudgetContext,
    ResearchConflict,
    ResearchContext,
    ResearchFormatError,
    ResearchUsage,
)
from botai.core.vault import VaultError
from botai.learn.author import Candidate
from botai.learn.intent import LearningIntent
from botai.learn.memory import DiscoveryMemory
from botai.settings import Settings


def digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def intent_key(intent: LearningIntent) -> str:
    data = intent.checkpoint()
    if intent.discovery_mode == "auto":
        # Inferred kinds are findings, not a new instruction from the student.
        data["requested_kinds"] = []
    return digest(data)


class SavedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Kind
    endpoint: dict[str, Any] = Field(repr=False)
    bootstrap: list[dict[str, Any]] = Field(default_factory=list, max_length=12, repr=False)
    auth: dict[str, Any]

    @classmethod
    def of(cls, value: Candidate) -> SavedCandidate:
        return cls(
            kind=value.kind,
            endpoint=copy.deepcopy(value.endpoint),
            bootstrap=copy.deepcopy(value.bootstrap),
            auth=copy.deepcopy(value.auth),
        )

    def candidate(self) -> Candidate:
        return Candidate(
            self.kind,
            copy.deepcopy(self.endpoint),
            copy.deepcopy(self.bootstrap),
            copy.deepcopy(self.auth),
        )


class ActivationReceipt(BaseModel):
    """Nonsecret commit acknowledgement; written with the recipe, independent of TTL."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    intent_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    spec_id: str
    revision: int = Field(ge=1)
    spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    kinds: list[Kind] = Field(max_length=8)
    relearn: bool


class ResearchProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    schema_version: Literal[1] = 1
    engine_version: Literal[1] = 1
    user_id: str
    job_id: str
    connection_id: str
    origin: str
    intent_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    allow_hosts_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    phase: Literal["observe", "author", "verify", "confirm", "activate"] = "observe"
    memory: DiscoveryMemory
    candidates: dict[Kind, SavedCandidate] = Field(default_factory=dict, max_length=8, repr=False)
    proposals: dict[Kind, Annotated[list[SavedCandidate], Field(max_length=3)]] = Field(
        default_factory=dict, max_length=8, repr=False
    )
    other_tokens: int = Field(default=0, ge=0)
    other_active_ms: int = Field(default=0, ge=0)
    other_requests: int = Field(default=0, ge=0)
    pending_tokens: int = Field(default=0, ge=0)
    pending_active_ms: int = Field(default=0, ge=0)
    uncertain_calls: int = Field(default=0, ge=0)
    # Kinds whose thin horizon the student was already asked about: once per research, even
    # across restarts and a changed intent.
    horizon_asked: list[Kind] = Field(default_factory=list, max_length=8)

    @property
    def tokens(self) -> int:
        return self.other_tokens + self.memory.tokens + self.memory.reserved_tokens

    @property
    def active_ms(self) -> int:
        return self.other_active_ms + self.memory.active_ms


class ResearchLedger:
    def __init__(
        self, ctx: ResearchContext, state: ResearchProgress, revision: int, settings: Settings
    ) -> None:
        self.ctx, self.state, self.revision, self.settings = ctx, state, revision, settings

    @classmethod
    async def load(
        cls,
        ctx: ResearchContext,
        settings: Settings,
        *,
        user_id: str,
        job_id: str,
        connection_id: str,
        origin: str,
        allow_hosts: tuple[str, ...],
        intent: LearningIntent,
        initial_tokens: int = 0,
    ) -> ResearchLedger:
        key, hosts_key = intent_key(intent), digest(sorted(allow_hosts))
        try:
            checkpoint = await ctx.load_research()
            usage = (
                await ctx.load_research_usage() if isinstance(ctx, ResearchBudgetContext) else None
            )
            old = (
                ResearchProgress.model_validate_json(checkpoint.payload.get_secret_value())
                if checkpoint is not None
                else None
            )
        except (ValidationError, ValueError, VaultError):
            raise BotaiError(
                ErrorCode.RECIPE_REJECTED, "Не удалось безопасно восстановить исследование"
            ) from None
        if old is not None:
            if (
                old.user_id != user_id
                or old.job_id != job_id
                or old.connection_id != connection_id
                or old.origin != origin
                or checkpoint is None
                or checkpoint.intent_key != old.intent_key
            ):
                raise BotaiError(
                    ErrorCode.RECIPE_REJECTED, "Состояние исследования не совпало с заданием"
                )
            try:
                old.memory.validate_for(origin, old.intent_key)
            except ValueError:
                raise BotaiError(
                    ErrorCode.RECIPE_REJECTED, "Состояние исследования повреждено"
                ) from None
            # A crash can fall between a remote response and its local commit. Keep the
            # reserved work charged; do not promise exactly-once provider calls.
            if old.pending_tokens or old.pending_active_ms:
                old.other_tokens += old.pending_tokens
                old.other_active_ms += old.pending_active_ms
                old.pending_tokens = old.pending_active_ms = 0
                old.uncertain_calls += 1
            if old.intent_key != key or old.allow_hosts_key != hosts_key:
                previous = old.memory
                old.memory = DiscoveryMemory(
                    origin=origin,
                    intent_key=key,
                    steps=previous.steps,
                    tokens=previous.tokens,
                    requests=previous.requests,
                    probes=previous.probes,
                    active_ms=previous.active_ms,
                    reserved_tokens=previous.reserved_tokens,
                    uncertain_calls=previous.uncertain_calls,
                    generations=previous.generations,
                )
                old.intent_key, old.allow_hosts_key = key, hosts_key
                old.candidates, old.proposals, old.phase = {}, {}, "observe"
            state = old
        else:
            state = ResearchProgress(
                user_id=user_id,
                job_id=job_id,
                connection_id=connection_id,
                origin=origin,
                intent_key=key,
                allow_hosts_key=hosts_key,
                memory=DiscoveryMemory(
                    origin=origin, intent_key=key, steps=usage.steps if usage else 0
                ),
                other_tokens=initial_tokens + (usage.tokens if usage else 0),
                other_active_ms=usage.active_ms if usage else 0,
                other_requests=usage.requests if usage else 0,
            )
        return cls(ctx, state, checkpoint.revision if checkpoint else 0, settings)

    def account_total(self, tokens: int) -> None:
        self.state.other_tokens = max(
            self.state.other_tokens,
            tokens - self.state.memory.tokens - self.state.memory.reserved_tokens,
        )

    def remaining_seconds(self) -> float:
        return max(0, self.settings.botai_research_max_seconds - self.state.active_ms / 1000)

    async def save(self) -> None:
        self.state.memory.validate_for(self.state.origin, self.state.intent_key)
        try:
            payload = SecretStr(self.state.model_dump_json())
            if isinstance(self.ctx, ResearchBudgetContext):
                checkpoint = await self.ctx.save_research_with_usage(
                    payload,
                    intent_key=self.state.intent_key,
                    expected_revision=self.revision,
                    usage=ResearchUsage(
                        tokens=self.state.tokens + self.state.pending_tokens,
                        active_ms=self.state.active_ms + self.state.pending_active_ms,
                        requests=self.state.memory.requests + self.state.other_requests,
                        steps=self.state.memory.steps,
                    ),
                )
            else:
                checkpoint = await self.ctx.save_research(
                    payload, intent_key=self.state.intent_key, expected_revision=self.revision
                )
        except ResearchConflict:
            raise JobDeferred("Продолжаю с обновлённого состояния") from None
        except (ValueError, VaultError, ResearchFormatError):
            raise BotaiError(
                ErrorCode.RECIPE_REJECTED, "Не удалось безопасно сохранить исследование"
            ) from None
        self.revision = checkpoint.revision
        diagnostic_event(
            "learn.research_saved",
            revision=self.revision,
            phase=self.state.phase,
            token_count=self.state.tokens,
            request_count=self.state.memory.requests + self.state.other_requests,
            elapsed_ms=self.state.active_ms,
            step=self.state.memory.steps,
            queued_count=len(self.state.memory.frontier),
            chosen_count=len(self.state.candidates),
            uncertain_calls=self.state.uncertain_calls + self.state.memory.uncertain_calls,
        )

    async def observed(self, memory: DiscoveryMemory) -> None:
        self.state.memory = memory.model_copy(deep=True)
        await self.save()

    async def before_request(self) -> None:
        if (
            self.state.other_requests + self.state.memory.requests
            >= self.settings.botai_research_max_requests
        ):
            raise BotaiError(
                ErrorCode.SOURCE_RATE_LIMITED, "Исследование достигло общего бюджета чтений"
            )
        self.state.other_requests += 1
        await self.save()

    @asynccontextmanager
    async def operation(
        self,
        phase: Literal["observe", "author", "verify", "confirm", "activate"],
        *,
        reserve_tokens: int = 0,
    ) -> AsyncGenerator[None]:
        state = self.state
        if state.pending_tokens or state.pending_active_ms:
            raise ValueError("research operation was not reconciled")
        if (
            reserve_tokens
            and state.tokens + reserve_tokens > self.settings.botai_research_max_tokens
        ):
            raise BotaiError(
                ErrorCode.QUOTA_EXCEEDED, "Исследование достигло общего бюджета модели"
            )
        seconds = min(300.0, self.remaining_seconds())
        if seconds <= 0:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Исследование достигло общего бюджета времени"
            )
        state.phase = phase
        state.pending_tokens, state.pending_active_ms = reserve_tokens, int(seconds * 1000)
        await self.save()
        started = time.monotonic()
        try:
            async with asyncio.timeout(seconds):
                yield
        except (JobClaimLost, JobInputChanged, asyncio.CancelledError):
            raise  # The next owner reconciles the persisted reservation.
        except BaseException as exc:
            state.other_active_ms += int((time.monotonic() - started) * 1000)
            state.other_tokens += state.pending_tokens
            state.pending_tokens = state.pending_active_ms = 0
            if reserve_tokens:
                state.uncertain_calls += 1
            await self.save()
            if isinstance(exc, TimeoutError):
                raise JobDeferred("Продолжаю исследование после ограничения времени") from None
            raise
        else:
            state.other_active_ms += int((time.monotonic() - started) * 1000)
            state.pending_tokens = state.pending_active_ms = 0
            await self.save()

    def llm(self, provider: LlmProvider) -> LlmProvider:
        return _ResearchLlm(self, provider)


class _ResearchLlm:
    def __init__(self, ledger: ResearchLedger, provider: LlmProvider) -> None:
        self.ledger, self.provider = ledger, provider

    async def complete(self, request: LlmRequest) -> LlmResponse:
        # UTF-8 request bytes conservatively bound input tokens; output is explicitly capped.
        reserve = len(request.model_dump_json().encode()) + request.max_tokens
        async with self.ledger.operation("author", reserve_tokens=reserve):
            response = await self.provider.complete(request)
            usage = response.usage
            self.ledger.state.other_tokens += (
                usage.input_cache_miss + usage.input_cache_hit + usage.output
            )
            self.ledger.state.pending_tokens = 0
        return response
