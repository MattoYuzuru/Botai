"""Adaptive reasoning effort of a run (D-044): light until real work, then the profile's.

Live run of 2026-11-12: a chat step at effort high costs 4–6 s, and most turns need nothing but
the study data; the same set at effort low took half the time with the same tool behaviour, while
thinking disabled made one turn explode. So a chat turn asks for `light_effort` while it calls only
the light tools and lets the purpose profile decide (`effort=None`) from the first other tool on,
also in a resumed run whose conversation already has such a call. The trace says which it was.
"""

from __future__ import annotations

from botai.agent.llm import load_profiles, resolve
from botai.agent.v4 import AgentRuntime, MemoryTraceStore, RunTask
from botai.agent.v4.chat import LIGHT_TOOLS
from botai.agent.v4.providers import MockProvider
from botai.agent.v4.runtime import heavy_calls
from botai.core.llm import LlmMessage, LlmRequest, LlmToolCall
from botai.settings import Settings
from tests.agent.v4.runtime_support import answer, call, handler_tool, limits, task, tool_response

LIGHT = frozenset({"study_data", "memory_list"})


async def _run(provider: MockProvider, run_task: RunTask) -> MemoryTraceStore:
    store = MemoryTraceStore()
    await AgentRuntime(monotonic=lambda: 0.0).run(
        run_task,
        provider,
        [handler_tool("study_data"), handler_tool("memory_list"), handler_tool("browser_navigate")],
        trace_store=store,
        limits=limits(max_steps=10),
    )
    return store


async def _model_efforts(store: MemoryTraceStore, run_id: str) -> list[str]:
    events = await store.read(run_id, limit=200)
    return [event.payload["effort"] for event in events if event.kind == "model_call"]


async def test_light_tools_keep_the_light_effort_and_a_heavy_one_escalates() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("study_data", {"text": "оценки"}, "c1")),
        tool_response(call("memory_list", {"text": "-"}, "c2")),
        tool_response(call("browser_navigate", {"text": "сайт"}, "c3")),
        tool_response(call("study_data", {"text": "ещё"}, "c4")),
        answer("готово"),
    )
    run_task = task(light_effort="low", light_tools=LIGHT)
    store = await _run(provider, run_task)

    assert [request.effort for request in provider.requests] == ["low", "low", "low", None, None]
    assert await _model_efforts(store, run_task.run_id) == [
        "low",
        "low",
        "low",
        "profile",
        "profile",
    ], "the trace says which effort each call asked for; a light tool after a heavy one stays"


async def test_without_a_light_effort_the_profile_decides_every_call() -> None:
    provider = MockProvider.from_responses(
        tool_response(call("study_data", {"text": "оценки"}, "c1")), answer("готово")
    )
    run_task = task(light_tools=LIGHT)
    store = await _run(provider, run_task)
    assert [request.effort for request in provider.requests] == [None, None]
    assert await _model_efforts(store, run_task.run_id) == ["profile", "profile"]


async def test_a_resumed_run_with_a_heavy_call_starts_escalated() -> None:
    history = (
        LlmMessage(role="user", content="подключи кабинет"),
        LlmMessage(
            role="assistant",
            content="",
            tool_calls=[LlmToolCall(id="old", name="browser_navigate", arguments={"text": "x"})],
        ),
        LlmMessage(role="tool", tool_call_id="old", content="открыто"),
    )
    assert heavy_calls(history, LIGHT)
    assert not heavy_calls(history[:1], LIGHT)
    provider = MockProvider.from_responses(answer("продолжил"))
    await _run(provider, task(messages=history, light_effort="low", light_tools=LIGHT))
    assert [request.effort for request in provider.requests] == [None]

    light_history = (
        LlmMessage(role="user", content="оценки?"),
        LlmMessage(
            role="assistant",
            content="",
            tool_calls=[LlmToolCall(id="old", name="study_data", arguments={"text": "x"})],
        ),
        LlmMessage(role="tool", tool_call_id="old", content="{}"),
    )
    provider = MockProvider.from_responses(answer("ответ"))
    await _run(provider, task(messages=light_history, light_effort="low", light_tools=LIGHT))
    assert [request.effort for request in provider.requests] == ["low"]


def test_the_request_effort_wins_and_none_falls_back_to_the_agent_profile() -> None:
    settings = Settings(_env_file=None, env="test", llm_profile_agent="deepseek-flash:enabled:high")
    profiles = load_profiles(settings)
    light = resolve(LlmRequest(messages=[], purpose="agent", effort="low"), settings, profiles)
    full = resolve(LlmRequest(messages=[], purpose="agent"), settings, profiles)
    assert (light.effort, full.effort) == ("low", "high")
    assert light.thinking == "enabled", "a light turn still thinks: disabled made one explode"


def test_the_chat_light_set_and_its_setting() -> None:
    assert {"study_data", "memory_remember", "ask_user", "source_list"} <= LIGHT_TOOLS
    assert not LIGHT_TOOLS & {"source_sync", "browser_navigate", "http_request", "code_exec"}
    assert Settings(_env_file=None, env="test").agent_chat_light_effort == "low"
    off = Settings(_env_file=None, env="test", BOTAI_AGENT_CHAT_LIGHT_EFFORT="")  # type: ignore[call-arg]
    assert off.agent_chat_light_effort == ""
