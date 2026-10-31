"""Worker entrypoint: `python -m botai.worker`. The loop itself is W1's (botai.jobs.runner)."""

from __future__ import annotations

import asyncio

from botai.agent.handlers import HANDLERS as AGENT
from botai.agent.v4.dispatch import make_v4_handlers
from botai.agent.v4.handlers import HANDLERS as AGENT_V4
from botai.browser.handlers import HANDLERS as BROWSER
from botai.core.handlers import HandlerTable
from botai.core.logging import configure_logging
from botai.learn.handlers import HANDLERS as LEARN
from botai.settings import Settings, get_settings
from botai.sources.handlers import HANDLERS as SOURCES


def handler_table(settings: Settings | None = None) -> HandlerTable:
    """The job table of this deployment: v3 tables by default, v4 engine takes source jobs over.

    With `BOTAI_AGENT_ENGINE=v4` the CONNECT/SYNC/REPAIR kinds are served by the v4 dispatch table
    (they keep a v3 bridge for connections that have no v4 source yet, see `agent.v4.dispatch`).
    With the default v3 flag the merged table is exactly the old one.
    """
    table: HandlerTable = {}
    for part in (BROWSER, SOURCES, LEARN, AGENT, AGENT_V4):
        overlap = table.keys() & part.keys()
        if overlap:
            raise RuntimeError(f"job kinds handled twice: {sorted(overlap)}")
        table.update(part)
    engine = (settings or get_settings()).agent_engine
    if engine == "v4":
        # An intentional override, not a duplicate: the v4 engine owns the source jobs.
        table.update(make_v4_handlers())
    return table


async def main() -> None:
    configure_logging()
    from botai.jobs.runner import run_forever  # W1

    await run_forever(handler_table())


if __name__ == "__main__":
    asyncio.run(main())
