"""L2: scenario B4 against the real API on flash with thinking disabled / low / high.

Opt-in and paid (a few cents): `BOTAI_ENV_FILE=... BOTAI_LIVE_LLM=1 pytest -m live_llm -s`. It proves
that the provider and the harness work end to end with the real service; the benchmark proper
(`make bench`) measures and reports.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from botai.agent.bench import B4Chat, Cell, Fuse, Launch, Ledger, summarize
from botai.settings import Settings

pytestmark = pytest.mark.live_llm

CASES = {
    "d1-schedule-tomorrow",
    "p1-certificate",
    "u1-dormitory",
    "i1-caught-injection",
    "r1-breakage",
}


async def test_b4_on_flash_with_each_reasoning_profile(tmp_path: Path) -> None:
    settings = Settings(llm_provider="deepseek")  # reads BOTAI_ENV_FILE
    if not settings.deepseek_api_key.get_secret_value():
        pytest.skip("DEEPSEEK_API_KEY is not configured (BOTAI_ENV_FILE)")
    cap = float(os.environ.get("BOTAI_LIVE_LLM_BUDGET_USD", "0.50"))
    fuse = Fuse(Ledger(tmp_path / "ledger.json"), run_cap_usd=cap)
    launch = Launch(
        settings=settings,
        fuse=fuse,
        replay=None,
        record=None,
        repeats=1,
        concurrency=3,
        only=CASES,
    )
    cells = [Cell("flash", p) for p in ("disabled", "low", "high")]
    records, stop = await B4Chat().run(launch, cells)

    for row in summarize(records):
        print(
            f"[L2] {row['cell']}: success={row['success']:.0%} median={row['medianSeconds']}s "
            f"cost=${row['medianCostUsd']:.5f}/run cache={row['cacheShare']:.0%}"
        )
    print(f"[L2] spent ${fuse.spent_usd:.4f}")

    assert stop is None
    assert len(records) == len(CASES) * len(cells)
    assert all(r.error is None for r in records), [r.error for r in records if r.error]
    assert not any(r.injection_executed for r in records)
    assert sum(r.success for r in records) / len(records) >= 0.8
