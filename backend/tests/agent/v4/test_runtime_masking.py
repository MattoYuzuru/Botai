"""Central masking: no tool output reaches the model, the checkpoint or the trace unmasked.

The live acceptance run caught the gap this closes: the agent's own parser printed the owner's
profile e-mail (a value the broker had issued as a login secret) and `code_exec` returned it
verbatim — browser tools masked, everything else did not. The runtime now applies the run's masker
to every `ToolResult` (summary, data trees, error text, artifact URLs) at the boundary, so
`code_exec`/`fs_*`/`http_request`/`trace_read` are covered by the same rule as `browser_*`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from botai.agent.v4 import AgentRuntime, MemoryTraceStore, RunLimits, RunTask
from botai.agent.v4.factory import build_agent_wiring, memory_trace_store
from botai.agent.v4.protocol import ToolContext, ToolResult, ToolSpec, object_schema
from botai.agent.v4.providers import MockProvider
from tests.agent.v4.agent_wiring_support import fake_services
from tests.agent.v4.runtime_support import answer, call, tool_response

SECRET = "P@ssw0rd-FIXTURE-MARKER"
MASK = "***"


def _mask(text: str) -> str:
    return text.replace(SECRET, MASK)


class LeakyTool:
    """A tool that returns a primary secret everywhere: summary, data, nested list and error."""

    spec = ToolSpec(
        name="code_exec",
        description="fixture tool",
        parameters=object_schema({"code": {"type": "string"}}, required=["code"]),
        policy="write_soft",
    )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success(
            summary=f"stdout: profile {SECRET}",
            data={"stdout": f"owner={SECRET}", "nested": [{"email": SECRET}]},
        )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def test_masking_covers_the_model_message_checkpoint_and_trace() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("code_exec", {"code": "print(...)"})),
        answer("готово"),
    )
    store = MemoryTraceStore()
    runtime = AgentRuntime(mask=_mask, monotonic=lambda: 0.0)
    result = await runtime.run(
        RunTask(task="запусти парсер", workspace="/tmp/masking-fixture"),
        provider,
        [LeakyTool()],
        trace_store=store,
        limits=RunLimits(max_steps=5),
    )

    assert result.status == "answer"
    tool_message = provider.requests[1].messages[-1]
    assert SECRET not in tool_message.content
    assert MASK in tool_message.content
    # The trace (what the owner/agent reads later) is masked too.
    assert SECRET not in _dump([event.payload for event in result.trace.events])
    # The checkpoint a resume would continue from is already masked.
    assert SECRET not in _dump([message.model_dump() for message in result.messages])

    # A resumed run starts from that checkpoint: the value cannot resurface through it.
    resumed_provider = MockProvider.from_responses(answer("продолжение"))
    resumed = await AgentRuntime(mask=_mask, monotonic=lambda: 0.0).run(
        RunTask(task="продолжи", messages=result.messages),
        resumed_provider,
        [],
        limits=RunLimits(max_steps=2),
    )
    assert resumed.status == "answer"
    assert SECRET not in _dump([m.model_dump() for m in resumed_provider.requests[0].messages])


async def test_wiring_masks_code_exec_stdout_with_the_secret_broker(tmp_path: Path) -> None:
    # The real product path: a v4 wiring whose broker has issued the secret, and a real LocalExecutor
    # running a parser that prints it. The model must see `***` in the tool result.
    services = fake_services()
    provider = MockProvider.from_responses(
        tool_response(
            call(
                "code_exec",
                {"language": "python", "code": f"print('profile {SECRET}')"},
                "c1",
            )
        ),
        answer("готово"),
    )
    store = memory_trace_store()
    wiring = await build_agent_wiring(
        services,
        user_id="u1",
        run_id="01M68Z000000000000000000B0",
        provider=provider,
        trace_store=store,
        with_browser=False,
        artifact_root=tmp_path / "artifacts",
    )
    try:
        assert wiring.broker is not None
        handle = await wiring.broker.issue(SECRET)
        assert SECRET not in handle
        result = await wiring.runtime.run(
            RunTask(
                task="запусти парсер профиля",
                workspace=str(wiring.workspace.root),
                user_id="u1",
            ),
            wiring.provider,
            wiring.tools,
            trace_store=store,
            limits=RunLimits(max_steps=4),
        )
    finally:
        await wiring.aclose()

    assert result.status == "answer"
    tool_message = provider.requests[1].messages[-1]
    assert SECRET not in tool_message.content
    assert MASK in tool_message.content
    assert SECRET not in _dump([event.payload for event in result.trace.events])
    assert SECRET not in _dump([m.model_dump() for m in result.messages])


async def test_a_plain_runtime_without_mask_is_unchanged() -> None:
    # The default constructor keeps old behaviour for callers that have no secrets (runtime L0 tests).
    provider = MockProvider.from_responses(
        tool_response(call("code_exec", {"code": "print(1)"})),
        answer(),
    )
    result = await AgentRuntime(monotonic=lambda: 0.0).run(
        RunTask(task="без секретов", workspace="/tmp/masking-fixture-2"),
        provider,
        [LeakyTool()],
        limits=RunLimits(max_steps=3),
    )
    assert result.status == "answer"
    assert SECRET in provider.requests[1].messages[-1].content
