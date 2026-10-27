"""Owner: W1. Poll `jobs` with SELECT ... FOR UPDATE SKIP LOCKED, run handlers with PgJobContext,
retries with backoff (lld.md §5), sync scheduler tick."""

from __future__ import annotations

from botai.core.handlers import HandlerTable


async def run_forever(handlers: HandlerTable) -> None:
    raise NotImplementedError("job runner is implemented in workstream W1")
