"""Job handlers owned by this module (see docs/plan/workstreams.md). Keep the HANDLERS name."""

from __future__ import annotations

from botai.core.handlers import HandlerTable
from botai.core.jobs import JobKind
from botai.sources.ingest import make_ingest_handler
from botai.sources.runner import make_sync_handler

HANDLERS: HandlerTable = {
    JobKind.SYNC: make_sync_handler(),
    JobKind.INGEST: make_ingest_handler(),
}
