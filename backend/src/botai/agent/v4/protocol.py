"""Tool protocol of the v4 free harness (docs/v4/02-architecture.md §4.3, D-022).

A tool is a `ToolSpec` — name, description, JSON Schema, timeout, policy class, idempotency,
result limit — plus a `ToolHandler` that turns validated arguments into a `ToolResult`.

The result is **data, not prose**: ``ok``, ``summary``, ``data``, ``artifacts``, ``truncated``,
``next``, ``error``. The runtime validates arguments against the schema before calling the
handler, so a malformed model call becomes a structured error result instead of an exception.

Secrets: a handler never returns a primary secret (password, MFA code, key link) in ``data``.
Session material (cookies, bearer tokens, storage) is working material the agent may read
(D-008); it is not a "primary secret" in the sense of the protocol.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, Self, runtime_checkable

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from botai.agent.v4.traces import TraceStore
from botai.core.llm import LlmTool

# --- policy and identity ------------------------------------------------------------------------

PolicyClass = Literal["read", "write_soft", "write_destructive", "money", "auth"]
"""What a call does, not whether it is allowed: the policy engine decides later (§4.4, D-006).

`read` — observation only; `write_soft` — a reversible write; `write_destructive` — deleting or
irreversible; `money` — spends money; `auth` — touches credentials/consent.
"""

ToolArguments = dict[str, Any]


class ToolSpec(BaseModel):
    """One tool as the model sees it. Frozen: the registry is data, not code (§4.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=4000)
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "additionalProperties": False, "properties": {}}
    )
    timeout_s: float = Field(default=30.0, gt=0, le=600)
    policy: PolicyClass = "read"
    idempotent: bool = False
    max_result_chars: int = Field(default=20_000, ge=200, le=2_000_000)
    # A tool that mutates shared state or cannot be cancelled safely never runs in parallel.
    serial_only: bool = False

    @field_validator("parameters")
    @classmethod
    def _is_object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("tool parameters must be a JSON object schema (type: object)")
        if value.get("additionalProperties") is not False:
            raise ValueError("tool parameters must set additionalProperties: false")
        return value

    @model_validator(mode="after")
    def _required_are_declared(self) -> Self:
        properties = self.parameters.get("properties") or {}
        required = self.parameters.get("required") or []
        missing = sorted(set(required) - set(properties))
        if missing:
            raise ValueError(f"required properties are not declared: {missing}")
        return self

    def parallel_safe(self) -> bool:
        """Read-only calls run concurrently; anything else keeps the model's order (§4.4)."""
        return self.policy == "read" and not self.serial_only

    def llm_tool(self) -> LlmTool:
        return LlmTool(
            name=self.name,
            description=self.description,
            parameters=json.loads(json.dumps(self.parameters)),
        )


def object_schema(
    properties: Mapping[str, Any] | None = None, *, required: Sequence[str] = ()
) -> dict[str, Any]:
    """Build a strict JSON Schema for tool arguments (all schemas set additionalProperties: false)."""
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties or {}),
    }
    if required:
        schema["required"] = list(required)
    return schema


# --- results ------------------------------------------------------------------------------------


class ToolError(BaseModel):
    """A structured, secret-free failure: the model can retry or change the approach (§4.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=80)
    detail: str = ""
    retryable: bool = False
    hint: str | None = None


class ToolArtifact(BaseModel):
    """A link to a big body/artifact that stays out of the model context (§4.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: str
    bytes: int = Field(default=0, ge=0)
    sha256: str = ""
    url: str | None = None


class ToolCost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ms: int = Field(default=0, ge=0)


class ToolResult(BaseModel):
    """What a handler returns: data for the model plus its own bookkeeping (§4.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool = True
    summary: str = ""
    data: Any = None
    artifacts: tuple[ToolArtifact, ...] = ()
    truncated: bool = False
    next: str | None = None
    error: ToolError | None = None
    cost: ToolCost = Field(default_factory=ToolCost)

    @model_validator(mode="after")
    def _error_matches_ok(self) -> Self:
        if self.ok and self.error is not None:
            raise ValueError("a successful ToolResult must not carry an error")
        if not self.ok and self.error is None:
            raise ValueError("a failed ToolResult must carry an error")
        return self

    @classmethod
    def success(
        cls,
        *,
        summary: str = "",
        data: Any = None,
        artifacts: Sequence[ToolArtifact] = (),
        truncated: bool = False,
        next: str | None = None,
        cost_ms: int = 0,
    ) -> ToolResult:
        return cls(
            ok=True,
            summary=summary,
            data=data,
            artifacts=tuple(artifacts),
            truncated=truncated,
            next=next,
            cost=ToolCost(ms=max(cost_ms, 0)),
        )

    @classmethod
    def failure(
        cls,
        code: str,
        detail: str = "",
        *,
        retryable: bool = False,
        hint: str | None = None,
        data: Any = None,
        cost_ms: int = 0,
    ) -> ToolResult:
        return cls(
            ok=False,
            summary=detail or code,
            data=data,
            error=ToolError(code=code, detail=detail, retryable=retryable, hint=hint),
            cost=ToolCost(ms=max(cost_ms, 0)),
        )

    def to_wire(self) -> dict[str, Any]:
        """The architecture shape of a result (§4.3)."""
        return {
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
            "artifacts": [artifact.model_dump(mode="json") for artifact in self.artifacts],
            "truncated": self.truncated,
            "next": self.next,
            "error": self.error.model_dump(mode="json") if self.error else None,
            "cost": self.cost.model_dump(mode="json"),
        }

    def to_model_text(self) -> str:
        return json.dumps(self.to_wire(), ensure_ascii=False, default=str)


# --- asking the person in the loop --------------------------------------------------------------


class AskQuestion(BaseModel):
    """A blocking question for the owner: options, free text or a protected input (§4.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    options: tuple[str, ...] = ()
    input_kind: Literal["text", "choice", "secret"] = "text"
    placeholder: str | None = None
    timeout_s: float | None = Field(default=None, gt=0, le=3600)


class AskAnswer(BaseModel):
    """The owner's answer. `text` may be a primary secret when `input_kind == "secret"`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = ""
    choice: int | None = Field(default=None, ge=0)
    cancelled: bool = False


AskUser = Callable[[AskQuestion], Awaitable[AskAnswer]]


# --- action policy (the engine itself is phase 3; the runtime only asks) ------------------------


class PolicyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    tool: str
    policy: PolicyClass
    arguments: ToolArguments


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool = True
    reason: str = ""


PolicyDecider = Callable[[PolicyRequest], Awaitable[PolicyDecision]]


# --- context ------------------------------------------------------------------------------------


@runtime_checkable
class RunControlView(Protocol):
    """What the runtime asks a run control; the worker owns the implementation (pause/stop, §4.5)."""

    @property
    def cancelled(self) -> bool: ...

    @property
    def paused(self) -> bool: ...


class ToolContext(BaseModel):
    """Per-call context: the run, its workspace, the ask channel and the trace store."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    task: str = ""
    workspace: Path = Path(".")
    step: int = 0
    user_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    trace_store: TraceStore | None = Field(default=None, exclude=True)
    ask: AskUser | None = Field(default=None, exclude=True)


class ToolHandler(Protocol):
    """A tool the runtime can execute: its spec plus the callable (§4.3)."""

    spec: ToolSpec

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult: ...


class FunctionTool:
    """Adapt a plain async function to `ToolHandler`."""

    def __init__(
        self,
        spec: ToolSpec,
        run: Callable[[ToolArguments, ToolContext], Awaitable[ToolResult]],
    ) -> None:
        self.spec = spec
        self._run = run

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return await self._run(arguments, context)


# --- registry -----------------------------------------------------------------------------------


def validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> list[str]:
    """Human-readable schema violations (empty = valid); at most five lines."""
    validator = Draft202012Validator(spec.parameters)
    problems: list[str] = []
    for error in sorted(validator.iter_errors(dict(arguments)), key=lambda e: str(e.absolute_path)):
        location = ".".join(str(part) for part in error.absolute_path) or "arguments"
        problems.append(f"{location}: {error.message}")
        if len(problems) >= 5:
            break
    return problems


class ToolRegistry:
    """Name -> handler, with the model-facing schemas in a stable order (§4.3)."""

    def __init__(self, tools: Iterable[ToolHandler]) -> None:
        registry: dict[str, ToolHandler] = {}
        for tool in tools:
            if tool.spec.name in registry:
                raise ValueError(f"duplicate tool name: {tool.spec.name}")
            registry[tool.spec.name] = tool
        self._tools = registry

    def get(self, name: str) -> ToolHandler | None:
        return self._tools.get(name)

    @property
    def specs(self) -> list[ToolSpec]:
        return sorted((tool.spec for tool in self._tools.values()), key=lambda spec: spec.name)

    def llm_tools(self) -> list[LlmTool]:
        return [spec.llm_tool() for spec in self.specs]

    def validate(self, name: str, arguments: Mapping[str, Any]) -> list[str]:
        tool = self._tools.get(name)
        if tool is None:
            return [f"unknown tool: {name}"]
        return validate_arguments(tool.spec, arguments)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools


__all__ = [
    "AskAnswer",
    "AskQuestion",
    "AskUser",
    "FunctionTool",
    "PolicyClass",
    "PolicyDecider",
    "PolicyDecision",
    "PolicyRequest",
    "RunControlView",
    "ToolArguments",
    "ToolArtifact",
    "ToolContext",
    "ToolCost",
    "ToolError",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "object_schema",
    "validate_arguments",
]
