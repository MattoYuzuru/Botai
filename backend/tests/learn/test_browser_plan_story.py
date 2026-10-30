"""Observed native control → authored plan ref → canary → activation → model-free replay."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import json
import re
from datetime import date, timedelta

import pytest
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from botai.browser.gateway import PlaywrightGateway
from botai.core.clock import FrozenClock
from botai.core.llm import LlmRequest, LlmResponse, ScriptedLlm
from botai.db import models as m
from tests.browser.conftest import tls as tls
from tests.fakeuni import data
from tests.fakeuni.login import COOKIE, SESSIONS
from tests.learn.conftest import EnvFactory
from tests.learn.test_rendered_story import DomSite, endpoint
from tests.learn.test_rendered_story import dom_site as dom_site
from tests.sources.helpers import NOW

pytestmark = pytest.mark.browser
ENTRY = data.ORIGIN + "/browser-plan-story"
TITLE = "Практикум по графам"


class PlanAuthor(ScriptedLlm):
    def __init__(self) -> None:
        super().__init__()
        self.used_refs: list[str] = []

    async def complete_observation(self, request: LlmRequest) -> LlmResponse:
        self.observation_requests.append(request)
        state = json.loads(request.messages[1].content)["observation"]
        if not state["browserPlans"]:
            capability = next(row for row in state["capabilities"] if row["kind"] == "set_date")
            assert any(value["ref"] == "nextWeek" for value in capability["values"])
            self.used_refs.append(capability["ref"])
            decision = {
                "stateRef": state["stateRef"],
                "action": "use",
                "ref": capability["ref"],
                "valueRef": "nextWeek",
                "discoveries": [],
                "evidenceRefs": [],
            }
        else:
            assert state["browserPlans"][0]["operations"] == ["set_date"]
            decision = {
                "stateRef": state["stateRef"],
                "action": "finish",
                "ref": None,
                "discoveries": ["scheduleEvents"],
                "evidenceRefs": [state["pages"][-1]["ref"]],
            }
        return LlmResponse(content=json.dumps(decision))

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        assert request.purpose == "learn"
        assert len(self.requests) == 1, request.messages[1].content.split("</observation>")[-1]
        text = request.messages[1].content
        references = re.findall(r"^\[browserPlan\] (bp[0-9]+) \|", text, flags=re.MULTILINE)
        assert references, "the author must see a plan produced by the real native capability"
        assert TITLE in text, "the observation must contain loaded records, not a busy shell"
        candidate = endpoint()
        candidate["request"] = {"method": "GET", "path": "/browser-plan-story"}
        candidate["browserPlanRef"] = references[-1]
        return LlmResponse(
            content=json.dumps(
                {
                    "found": True,
                    "bootstrap": [],
                    "endpoints": [candidate],
                }
            )
        )


async def test_observed_date_plan_is_compiled_activated_and_replayed_on_a_fresh_browser_without_llm(
    make_env: EnvFactory,
    dom_site: DomSite,
) -> None:
    reads: list[str] = []
    writes: list[str] = []

    @dom_site.app.get("/browser-plan-story", response_model=None)
    async def page(request: Request) -> HTMLResponse | RedirectResponse:
        if request.cookies.get(COOKIE) not in SESSIONS:
            return RedirectResponse("/auth/login", status_code=302)
        return HTMLResponse("""<!doctype html><main>
<label>Начало периода<input id="period" type="date" value="2026-10-01" aria-controls="results"></label>
<section id="results" role="region" aria-label="Расписание"><p class="shell">Выберите период</p></section>
<script>
document.getElementById('period').onchange=async e=>{
 const rows=document.getElementById('results'); rows.setAttribute('aria-busy','true');
 rows.innerHTML='<p class="shell">Загружаю расписание</p>';
 const body=await(await fetch('/browser-plan-data?begin_date='+encodeURIComponent(e.target.value))).text();
 await new Promise(resolve=>setTimeout(resolve,80));
 rows.innerHTML=body; rows.setAttribute('aria-busy','false');
};
fetch('/browser-plan-write',{method:'POST'}).catch(()=>{});
</script></main>""")

    @dom_site.app.get("/browser-plan-data")
    async def rows(begin_date: str) -> HTMLResponse:
        date.fromisoformat(begin_date)
        reads.append(begin_date)
        await asyncio.sleep(0.08)
        return HTMLResponse(
            f'<article class="event"><h4 class="event-title">{TITLE}</h4>'
            f'<time class="begin" datetime="{begin_date}T09:00:00+03:00">09:00</time>'
            f'<time class="end" datetime="{begin_date}T10:20:00+03:00">10:20</time></article>'
        )

    @dom_site.app.post("/browser-plan-write")
    async def write() -> dict[str, bool]:
        writes.append("unexpected")
        return {"ok": True}

    env = await make_env([])
    await env.login()
    planner = PlanAuthor()
    env.services.llm = planner
    env.services.browser = dom_site.gateway
    clock = env.services.clock
    assert isinstance(clock, FrozenClock)
    dom_site.gateway._clock = clock

    result = await env.run(payload={"url": ENTRY, "kinds": ["scheduleEvents"]})
    assert result["status"] == "ok"
    assert len(planner.observation_requests) == 2
    assert len(planner.requests) == 1
    assert len(planner.used_refs) == 1
    assert await env.specs() == [(1, "active")]
    stored = await env.spec(1)
    assert stored["recipe"]["version"] == 2
    request = stored["recipe"]["endpoints"][0]["request"]
    assert request["path"] == "/browser-plan-story"
    assert request["render"] is True
    step = request["browserPlan"]["steps"][0]
    assert step["op"] == "set_date"
    assert step["value"] == "{plusDays:7}"
    assert step["target"]["selector"] == "#period"
    assert step["region"]["selector"] == "#results"
    assert "2026-10-01" not in json.dumps(request["browserPlan"])
    assert stored["recipe"]["endpoints"][0]["response"]["itemsSelector"] == "article.event"
    assert "browserPlanRef" not in json.dumps(stored)
    assert planner.used_refs[0] not in json.dumps(stored)
    statement = select(m.ScheduleEvent).where(
        m.ScheduleEvent.connection_id == env.seeded.connection_id
    )
    first = (await env.db.scalars(statement)).one()
    expected_first = (NOW + timedelta(days=7)).date().isoformat()
    assert first.starts_at.date().isoformat() == expected_first
    assert first.title == TITLE
    first_id = first.id
    assert reads
    assert set(reads) == {expected_first}  # discovery, canaries and first sync
    assert writes == []
    assert dom_site.gateway.active_for(env.seeded.user_id) is None

    await dom_site.gateway.aclose()
    clock.advance(timedelta(days=7))
    fresh = PlaywrightGateway(
        dom_site.gateway._egress, clock=clock, sandbox=False, ignore_https_errors=True
    )
    no_model = ScriptedLlm()
    env.services.browser = fresh
    env.services.llm = no_model
    try:
        assert (await env.sync())["status"] == "ok"
        assert reads[-1] == (NOW + timedelta(days=14)).date().isoformat()
        env.db.expire_all()
        records = list(await env.db.scalars(statement))
        assert len(records) == 2
        by_date = {record.starts_at.date().isoformat(): record.id for record in records}
        assert by_date[expected_first] == first_id
        assert (await env.sync())["status"] == "ok"
        env.db.expire_all()
        assert {
            record.starts_at.date().isoformat(): record.id
            for record in await env.db.scalars(statement)
        } == by_date
        assert no_model.requests == no_model.observation_requests == []
        assert len(planner.requests) == 1
        assert writes == []
        assert fresh.active_for(env.seeded.user_id) is None
    finally:
        await fresh.aclose()
