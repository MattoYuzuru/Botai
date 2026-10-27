"""Owner: W5. Agent harness (docs/architecture/agent-harness.md). Keep the signatures.

Learning (W3) does not need the tool loop: it calls `services.llm.complete` with `purpose="learn"`
and a JSON schema. The provider resolves the profile (model, thinking, effort) from the purpose
when the request leaves them unset.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from botai.core.jobs import JobContext
from botai.core.llm import LlmMessage, LlmTool, LlmUsage, Purpose
from botai.core.services import Services

ToolFn = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(slots=True)
class HarnessTool:
    spec: LlmTool
    run: ToolFn  # returns data for the model; wrapped as <source_data> by the harness


@dataclass(slots=True)
class HarnessResult:
    content: str
    steps: int
    usage: list[LlmUsage] = field(default_factory=list)
    stopped: str = "answer"  # answer | budget | quota | error


async def run(
    purpose: Purpose,
    *,
    skill: str,
    messages: Sequence[LlmMessage],
    tools: Sequence[HarnessTool],
    ctx: JobContext | None,
    services: Services,
    user_id: str,
    job_id: str | None = None,
) -> HarnessResult:
    raise NotImplementedError("W5")
