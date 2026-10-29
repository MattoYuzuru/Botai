"""The observed request names the page whose script issued it: the source of the open_url."""

from __future__ import annotations

import pytest

from tests.browser.test_gateway import Opener, log_in, open_browser  # noqa: F401  (fixture)
from tests.fakeuni import slide

pytestmark = pytest.mark.browser
PAGE = f"https://fakeuni.test{slide.PAGE_PATH}?begin_date=2026-10-26"


async def test_an_xhr_remembers_the_page_that_issued_it(open_browser: Opener) -> None:  # noqa: F811
    handle = await open_browser()
    await log_in(handle)
    await handle.goto(PAGE)
    log = await handle.network_log()
    schedule = next(r for r in log if r.url.split("?")[0].endswith(slide.API_PATH))
    assert schedule.initiator_url == PAGE
    assert schedule.resource_type in {"fetch", "xhr"}
    # the page itself was opened by the browser, not by a script of another page
    assert any(r.resource_type == "document" and r.url == PAGE for r in log)
