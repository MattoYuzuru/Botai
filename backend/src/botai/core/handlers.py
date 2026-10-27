from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from botai.core.jobs import JobContext, JobKind
from botai.core.services import Services

# A job handler gets the context (to talk to the user), the services and the job payload.
# It returns a JSON-able result or raises BotaiError / JobCancelled.
Handler = Callable[[JobContext, Services, dict[str, Any]], Awaitable[dict[str, Any] | None]]
HandlerTable = dict[JobKind, Handler]
