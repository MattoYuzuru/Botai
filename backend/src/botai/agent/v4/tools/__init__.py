"""Built-in v4 tools (plan 1.2): HTTP, files, asking the owner, reading traces.

Later phases add `browser_*`, `code_exec`, `secret_*`, `source_*`, `rag_*`, `memory_*`, `job_*`
by registering more `ToolHandler`s; the runtime does not change. Tool handlers are stateless
except for injected transports/executors — the run state lives in `ToolContext`.
"""

from __future__ import annotations

from botai.agent.v4.protocol import ToolHandler
from botai.agent.v4.tools.ask import AskUserTool
from botai.agent.v4.tools.fs import FsListTool, FsReadTool, FsWriteTool
from botai.agent.v4.tools.http import HttpRequestTool
from botai.agent.v4.tools.traces import TraceReadTool


def build_basic_tools() -> list[ToolHandler]:
    """The phase-1 tool set, in a stable order (the runtime sorts schemas by name anyway)."""
    return [
        HttpRequestTool(),
        FsListTool(),
        FsReadTool(),
        FsWriteTool(),
        AskUserTool(),
        TraceReadTool(),
    ]


__all__ = [
    "AskUserTool",
    "FsListTool",
    "FsReadTool",
    "FsWriteTool",
    "HttpRequestTool",
    "TraceReadTool",
    "build_basic_tools",
]
