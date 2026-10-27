"""Job handlers owned by this module (see docs/plan/workstreams.md). Keep the HANDLERS name."""

from __future__ import annotations

from botai.agent.chat import handle_chat
from botai.core.handlers import HandlerTable
from botai.core.jobs import JobKind

HANDLERS: HandlerTable = {JobKind.CHAT: handle_chat}
