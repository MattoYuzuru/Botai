"""Worker entrypoint: `python -m botai.worker`. The loop itself is W1's (botai.jobs.runner)."""

from __future__ import annotations

import asyncio

from botai.agent.handlers import HANDLERS as AGENT
from botai.browser.handlers import HANDLERS as BROWSER
from botai.core.handlers import HandlerTable
from botai.core.logging import configure_logging
from botai.learn.handlers import HANDLERS as LEARN
from botai.sources.handlers import HANDLERS as SOURCES


def handler_table() -> HandlerTable:
    table: HandlerTable = {}
    for part in (BROWSER, SOURCES, LEARN, AGENT):
        overlap = table.keys() & part.keys()
        if overlap:
            raise RuntimeError(f"job kinds handled twice: {sorted(overlap)}")
        table.update(part)
    return table


async def main() -> None:
    configure_logging()
    from botai.jobs.runner import run_forever  # W1

    await run_forever(handler_table())


if __name__ == "__main__":
    asyncio.run(main())
