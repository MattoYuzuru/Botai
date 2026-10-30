"""L2: the real model researches the synthetic SPA cabinet whose "my classes" page shows RULES.

Opt-in and paid: `BOTAI_LIVE_LLM=1 BOTAI_ENV_FILE=<.env> BOTAI_LIVE_LLM_BUDGET_USD=1.5 uv run
--frozen pytest tests/learn/test_lms_live.py -s`. Real Chromium reads the cabinet through the
EgressProxy; the model is the one production uses. `BOTAI_LIVE_RECORD_DIR` keeps every exchange
(request messages and answer) as JSON for building the scripted answers of the L1 story
(`test_lms_story.py`); the key is read by pydantic-settings only and never printed.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from botai.agent.bench import NullGate
from botai.agent.llm import DeepSeekProvider, MeteredLlm
from botai.core.jobs import JobCancelled
from botai.core.llm import LlmRequest, LlmResponse
from botai.db import models as m
from botai.learn import explorer
from botai.settings import Settings
from tests.browser.conftest import Tls, tls  # noqa: F401
from tests.fakeuni import lms, login
from tests.learn.conftest import EnvFactory
from tests.learn.lms_env import ENTRY, LmsSite, serving_lms

pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("BOTAI_LIVE_LLM") != "1", reason="L2: set BOTAI_LIVE_LLM=1 to spend money"
    ),
]


class Live:
    """The production provider, metered against a cap, with every exchange kept for inspection."""

    def __init__(self, cap_usd: float, directory: Path | None) -> None:
        settings = Settings(llm_provider="deepseek")
        if not settings.deepseek_api_key.get_secret_value():
            pytest.skip("DEEPSEEK_API_KEY is not configured (BOTAI_ENV_FILE)")
        self.meter = MeteredLlm(DeepSeekProvider(settings, gate=NullGate()), cap_usd=cap_usd)
        self.directory = directory
        self.sequence = 0

    @property
    def spent(self) -> float:
        return self.meter.spent_usd

    async def complete(self, request: LlmRequest) -> LlmResponse:
        response = await self.meter.complete(request)
        self.sequence += 1
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / f"{self.sequence:02d}-{request.purpose}.json").write_text(
                json.dumps(
                    {
                        "purpose": request.purpose,
                        "fingerprint": request.fingerprint(),
                        "messages": [m_.model_dump(mode="json") for m_ in request.messages],
                        "response": response.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                "utf-8",
            )
        return response

    async def complete_observation(self, request: LlmRequest) -> LlmResponse:
        return await self.complete(request)


@pytest.fixture
async def lms_site(tls: Tls) -> AsyncIterator[LmsSite]:  # noqa: F811
    async with serving_lms(tls) as site:
        yield site


async def test_the_real_model_researches_the_rules_cabinet(
    make_env: EnvFactory, lms_site: LmsSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = os.environ.get("BOTAI_LIVE_RECORD_DIR")
    live = Live(
        float(os.environ.get("BOTAI_LIVE_LLM_BUDGET_USD", "0.50")),
        Path(record) if record else None,
    )
    trace: list[str] = []

    original_verify = explorer.Explorer._verify_candidate  # pyright: ignore[reportPrivateUsage]

    async def verify(self: Any, kind: Any, candidate: Any, index: int, *args: Any) -> Any:
        checked, findings = await original_verify(self, kind, candidate, index, *args)
        trace.append(
            f"[verify] {kind} #{index} -> {'ok' if checked else 'REJECTED'}; findings={findings}"
        )
        trace.append("[candidate] " + json.dumps(candidate.endpoint, ensure_ascii=False))
        return checked, findings

    monkeypatch.setattr(explorer.Explorer, "_verify_candidate", verify)
    monkeypatch.setattr(login, "ROOT_TARGET", "/learn")  # the cabinet is the whole site
    env = await make_env([])
    await env.login()
    env.services.browser = lms_site.gateway
    env.services.llm = live  # type: ignore[assignment]
    result: Any = None
    try:
        result = await env.run(payload={"url": ENTRY}, choices=["skip"])
    except JobCancelled as exc:  # the scripted student never answers a question
        result = f"asked the student: {exc}"
    finally:
        print(f"\n[L2] spent=${live.spent:.4f} calls={len(live.meter.calls)}")
        for line in trace:
            print(line)
        print("[ids segments asked]", lms.STATE.segments)
        print("[task state reads]", lms.STATE.state_reads)
        for block in env.ctx.blocks:
            print(
                "[block]",
                json.dumps(block.model_dump(mode="json", by_alias=True), ensure_ascii=False)[:600],
            )
    print("[result]", result)
    print("[specs]", await env.specs())
    counts = {
        model.__tablename__: await env.count(model) for model in (m.ScheduleEvent, m.Deadline)
    }
    print("[rows]", counts)

    # What a good answer to this cabinet is; the model is free in how it writes the rest.
    assert isinstance(result, dict), result
    assert result["status"] == "ok"
    endpoints = {e["kind"]: e for e in (await env.spec(1))["recipe"]["endpoints"]}
    print("[stored spec]", json.dumps(await env.spec(1), ensure_ascii=False))
    assert endpoints["scheduleEvents"]["response"]["expand"]["type"] == "weekly"
    assert counts["schedule_events"] == 36  # 5 rules unrolled over the weeks of the term
    assert endpoints["deadlines"]["request"]["query"] == {"state": list(lms.STATES)}
    assert counts["deadlines"] == 5
    assert set(lms.STATE.segments) == {"me"}
    assert all(asked == list(lms.STATES) for asked in lms.STATE.state_reads)
