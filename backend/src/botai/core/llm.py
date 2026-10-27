"""LLM provider protocol (implementation: botai.agent.llm, W5) and a scripted test double.

Rule: no request ever contains a password, cookie, token or MFA code (NFR-004). Callers build
messages from data, never from Vault values.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]
Tier = Literal["fast", "pro"]
Thinking = Literal["enabled", "disabled"]
Effort = Literal["low", "high", "max"]
Purpose = Literal["chat", "classify", "learn", "repair", "extract", "summarize", "bench"]


class LlmToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class LlmMessage(BaseModel):
    role: Role
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[LlmToolCall] = Field(default_factory=list)
    # DeepSeek thinking mode: with tools, reasoning_content of every previous assistant turn
    # must be sent back, otherwise the API returns 400 (docs/architecture/agent-harness.md §1).
    reasoning_content: str | None = None


class LlmTool(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class LlmRequest(BaseModel):
    tier: Tier = "fast"
    model: str | None = None  # overrides tier -> model mapping (benchmark, profiles)
    thinking: Thinking | None = None  # None = provider default (enabled)
    effort: Effort | None = None  # None = provider default (high)
    messages: list[LlmMessage]
    tools: list[LlmTool] = Field(default_factory=list)
    json_schema: dict[str, Any] | None = None  # ask for a JSON object matching this schema
    max_tokens: int = 2000
    temperature: float = 0.2
    purpose: Purpose = "chat"  # for usage accounting and profiles

    def fingerprint(self) -> str:
        """Stable hash of what the model sees (purpose excluded)."""
        payload = self.model_dump(exclude={"purpose", "max_tokens"}, mode="json")
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class LlmUsage(BaseModel):
    input_cache_miss: int = 0
    input_cache_hit: int = 0
    output: int = 0
    reasoning: int = 0  # part of output spent on reasoning, if the provider reports it
    model: str = ""
    latency_ms: int = 0


class LlmResponse(BaseModel):
    content: str = ""
    reasoning_content: str | None = None
    tool_calls: list[LlmToolCall] = Field(default_factory=list)
    usage: LlmUsage = Field(default_factory=LlmUsage)

    def json(self) -> Any:  # type: ignore[override]
        return json.loads(self.content)


class LlmProvider(Protocol):
    async def complete(self, request: LlmRequest) -> LlmResponse: ...


class LlmFixtureMissing(Exception):
    pass


class ScriptedLlm:
    """Replays recorded responses by request fingerprint, or by an explicit queue.

    Fails loudly on an unknown request — a silent empty answer would hide bugs.
    """

    def __init__(
        self,
        by_fingerprint: Mapping[str, LlmResponse] | None = None,
        queue: Sequence[LlmResponse] | None = None,
    ) -> None:
        self._by_fp = dict(by_fingerprint or {})
        self._queue = list(queue or [])
        self.requests: list[LlmRequest] = []

    @classmethod
    def from_dir(cls, path: Path) -> ScriptedLlm:
        table: dict[str, LlmResponse] = {}
        for f in sorted(path.glob("*.json")):
            data = json.loads(f.read_text())
            table[data["fingerprint"]] = LlmResponse.model_validate(data["response"])
        return cls(by_fingerprint=table)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        fp = request.fingerprint()
        if fp in self._by_fp:
            return self._by_fp[fp]
        if self._queue:
            return self._queue.pop(0)
        raise LlmFixtureMissing(
            f"no scripted response for request {fp} (purpose={request.purpose})"
        )
