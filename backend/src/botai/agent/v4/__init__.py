"""BotAI v4 agent runtime: one free harness for every task (D-001, D-021).

The package is isolated from the v3 agent (`agent/harness.py`, `agent/tools*.py` stay untouched):
phase 1 builds the loop, the tool protocol, providers and traces here, and the v3 path remains the
default until the `agent_engine=v4` wiring is accepted (docs/v4/05-implementation-plan.md).

Public entry points:

* `botai.agent.v4.runtime.AgentRuntime` — the loop "model -> tool calls -> results -> answer";
* `botai.agent.v4.protocol` — `ToolSpec`, `ToolResult`, `ToolContext`, `ToolHandler`, registry;
* `botai.agent.v4.traces` — `TraceEvent`, `RunTrace`, `TraceStore`, `MemoryTraceStore`;
* `botai.agent.v4.providers` — `Provider` protocol and `MockProvider`; concrete providers are
  built by `botai.agent.v4.providers.factory.build_provider`;
* `botai.agent.v4.tools` — `http_request`, `fs_read/write/list`, `ask_user`, `trace_read`.

Everything below the wiring is DB-free: the runtime talks to protocols (`Provider`, `TraceStore`,
`AskUser`, `RunControlView`) and receives them from the caller (architecture §3.1, §4).
"""

from __future__ import annotations

from botai.agent.v4.protocol import (
    AskAnswer,
    AskQuestion,
    AskUser,
    FunctionTool,
    PolicyDecider,
    PolicyDecision,
    PolicyRequest,
    RunControlView,
    ToolContext,
    ToolHandler,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    object_schema,
)
from botai.agent.v4.runtime import (
    AgentRuntime,
    ManualControl,
    RunLimits,
    RunResult,
    RunTask,
)
from botai.agent.v4.traces import (
    MemoryTraceStore,
    RunInfo,
    RunStatus,
    RunTrace,
    TraceEvent,
    TraceStore,
)

__all__ = [
    "AgentRuntime",
    "AskAnswer",
    "AskQuestion",
    "AskUser",
    "FunctionTool",
    "ManualControl",
    "MemoryTraceStore",
    "PolicyDecider",
    "PolicyDecision",
    "PolicyRequest",
    "RunControlView",
    "RunInfo",
    "RunLimits",
    "RunResult",
    "RunStatus",
    "RunTask",
    "RunTrace",
    "ToolContext",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "TraceEvent",
    "TraceStore",
    "object_schema",
]
