"""Operator smoke on a fixed public query, through the same search service and EgressProxy."""

from __future__ import annotations

import argparse
import asyncio
import json

from botai.browser.egress import close_egress
from botai.search import SearchService
from botai.settings import get_settings


async def smoke(provider: str) -> int:
    settings = get_settings().model_copy(update={"search_mode": provider})
    service = SearchService(settings)
    try:
        result = await service.search("official university website")
        print(
            json.dumps(
                {
                    "provider": provider,
                    "status": result.status,
                    "results": len(result.results),
                    "issues": [
                        {"provider": issue.provider, "reason": issue.reason}
                        for issue in result.issues
                    ],
                }
            )
        )
        return 0 if result.status in {"ok", "empty"} else 1
    finally:
        await close_egress()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("auto", "exa", "deepseek"), default="auto")
    args = parser.parse_args()
    return asyncio.run(smoke(args.provider))


if __name__ == "__main__":
    raise SystemExit(main())
