"""Token estimates. DeepSeek has no public tokenizer, so these are conservative approximations.

Used for budgets, thread memory and the context meter, never for billing: billing comes from the
`usage` the API returns.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from botai.core.llm import LlmMessage, LlmTool

CHARS_PER_TOKEN = 3.0  # Russian text with JSON/Markdown overhead; English is closer to 4


def estimate_tokens(text: str) -> int:
    return -(-len(text) // 3) if text else 0


def estimate_message(message: LlmMessage) -> int:
    n = estimate_tokens(message.content) + 4  # role / separators
    for call in message.tool_calls:
        n += estimate_tokens(call.name) + estimate_tokens(json.dumps(call.arguments)) + 4
    if message.reasoning_content:
        n += estimate_tokens(message.reasoning_content)
    return n


def estimate_messages(messages: Iterable[LlmMessage]) -> int:
    return sum(estimate_message(m) for m in messages)


def estimate_tools(tools: Iterable[LlmTool]) -> int:
    return sum(estimate_tokens(json.dumps(t.model_dump(), ensure_ascii=False)) for t in tools)
