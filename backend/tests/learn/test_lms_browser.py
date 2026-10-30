"""The rules cabinet in a real Chromium: what the page's own scripts request is what research sees.

Same stand and same author answers as `test_lms_story.py`, but the observation comes from a real
browser through the EgressProxy, so the repeated key, the cookie-only session and the page that
issued each request are the ones a student's browser produces. The model is scripted.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import pytest

from botai.db import models as m
from tests.browser.conftest import Tls, tls  # noqa: F401
from tests.fakeuni import lms, login
from tests.learn.conftest import EnvFactory
from tests.learn.lms_env import ENTRY, STATE_QUERY, LmsSite, serving_lms
from tests.learn.test_lms_story import (
    Researcher,
    author,
    courses_endpoint,
    deadlines_endpoint,
    events,
    schedule_endpoint,
)

pytestmark = pytest.mark.browser


@pytest.fixture
async def lms_site(tls: Tls) -> AsyncGenerator[LmsSite]:  # noqa: F811
    async with serving_lms(tls) as site:
        yield site


async def test_a_real_browser_shows_research_the_repeated_key_and_the_rules(
    make_env: EnvFactory, lms_site: LmsSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login, "ROOT_TARGET", "/learn")
    study = await make_env([])
    await study.login()
    study.services.browser = lms_site.gateway
    # no scripted planner: the deterministic walk along the menu opens both pages
    study.services.llm = Researcher(
        [author(schedule_endpoint()), author(deadlines_endpoint()), author(courses_endpoint())],
        discovery=[],
    )
    result = await study.run(
        payload={"url": ENTRY, "kinds": ["scheduleEvents", "deadlines", "courses"]}
    )
    assert result["status"] == "ok", result

    prompt = study.services.llm.requests[0].messages[1].content
    assert f"[request] GET {lms.TASKS_PATH}?{STATE_QUERY} -> 200" in prompt
    assert f"[request] GET {lms.TIMETABLES_PATH} -> 200" in prompt
    assert "email" not in json.dumps(await study.spec(1))
    assert len(await events(study)) == 36
    assert await study.count(m.Deadline) == 5
    assert all(asked == list(lms.STATES) for asked in lms.STATE.state_reads)
    assert set(lms.STATE.segments) == {"me"}
