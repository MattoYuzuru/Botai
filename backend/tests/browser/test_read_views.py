"""Only explicitly observed safe tab panels can be activated in a read-only browser."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Route

from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.browser.read_views import ReadViewBrowser
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id

pytestmark = pytest.mark.browser
ORIGIN = "https://fakeuni.test"
HTML = """<!doctype html><body>
<div id="courses" role="tab" aria-controls="panel-courses">Курсы</div>
<div id="schedule" role="tab" aria-controls="panel-schedule" onclick="
  document.getElementById('panel-schedule').textContent = 'Пары открыты';
  fetch('/write', {method:'POST'}).then(() => { document.body.dataset.write = 'sent'; })
  .catch(() => { document.body.dataset.write = 'blocked'; });">Расписание</div>
<div id="panel-courses" role="tabpanel">Учебные материалы</div>
<div id="panel-schedule" role="tabpanel">Пары скрыты</div>
<form><button type="button" role="tab" aria-controls="panel-courses">Форма</button></form>
<button type="submit" role="tab" aria-controls="panel-courses">Submit control</button>
<div role="tab" aria-controls="missing">Несуществующая панель</div>
<div role="tab" aria-controls="panel-courses" aria-disabled="true">Недоступно</div>
<div role="tab" aria-controls="panel-courses" style="display:none">Скрыто</div>
<div role="tab" aria-controls="panel-courses">Удалить курс</div>
<a role="tab" aria-controls="panel-courses" href="/delete">Другая ссылка</a>
<div role="tab" aria-controls="panel-courses">Поле<input></div>
</body>"""


@pytest.fixture
async def tabs(gateway: PlaywrightGateway) -> AsyncIterator[PlaywrightHandle]:
    browser = await gateway.open(
        user_id=new_id(), connection_id=new_id(), allow_hosts=("fakeuni.test",), read_only=True
    )

    async def page(route: Route) -> None:
        await route.fulfill(content_type="text/html", body=HTML)

    await browser.context.route(ORIGIN + "/tabs", page)
    await browser.context.route(ORIGIN + "/other", page)
    await browser.goto(ORIGIN + "/tabs")
    try:
        yield browser
    finally:
        await browser.close()


async def test_discovery_exposes_only_safe_tab_refs(tabs: PlaywrightHandle) -> None:
    assert isinstance(tabs, ReadViewBrowser)
    views = await tabs.read_views()
    assert [view.label for view in views] == ["Курсы", "Расписание"]
    assert all(view.ref.startswith("tab-") for view in views)
    assert all("#" not in view.ref and "selector" not in view.ref for view in views)


async def test_named_tab_changes_view_but_does_not_enable_network_writes(
    tabs: PlaywrightHandle,
) -> None:
    view = next(view for view in await tabs.read_views() if view.label == "Расписание")
    snapshot = await tabs.activate_read_view(view.ref)
    assert "Пары открыты" in snapshot.text
    await tabs.page.wait_for_function("document.body.dataset.write === 'blocked'")
    with pytest.raises(BotaiError) as exc:
        await tabs.click("#schedule")
    assert exc.value.code == ErrorCode.CONFLICT


@pytest.mark.parametrize("action", ["invalid_ref", "snapshot", "navigation", "replacement"])
async def test_stale_or_replaced_targets_cannot_be_activated(
    tabs: PlaywrightHandle, action: str
) -> None:
    view = next(view for view in await tabs.read_views() if view.label == "Расписание")
    ref = view.ref
    if action == "invalid_ref":
        ref = "#schedule"
    elif action == "snapshot":
        await tabs.snapshot()
    elif action == "navigation":
        await tabs.goto(ORIGIN + "/other")
    else:
        await tabs.page.evaluate(
            "() => { const old = document.getElementById('schedule');"
            "old.replaceWith(old.cloneNode(true)); }"
        )
    with pytest.raises(BotaiError) as exc:
        await tabs.activate_read_view(ref)
    assert exc.value.code == ErrorCode.CONFLICT
