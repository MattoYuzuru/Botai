"""Native filter capabilities and fresh replay over fixture-only, guarded Chromium pages."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Response, Route

from botai.browser import read_capabilities as capabilities_module
from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.browser.read_capabilities import ReadCapabilityRegistry, ReadEffectError
from botai.contracts.browser_plan import (
    BrowserPlan,
    ReadFrame,
    ReadTarget,
)
from botai.core.ids import new_id
from botai.core.read_capabilities import ReadCapability

pytestmark = pytest.mark.browser
ORIGIN = "https://fakeuni.test"
URL = ORIGIN + "/native-read"
NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)

HTML = """<!doctype html><main>
<label>Начало периода<input id="from" type="date" value="2026-10-20" aria-controls="rows"></label>
<label>Вид занятий<select id="kind" aria-controls="rows"><option value="all">Все</option><option value="lectures">Лекции</option></select></label>
<section id="rows" role="region" aria-label="Результаты"><article class="lesson">До фильтра</article></section>
<button id="tab" type="button" role="tab" aria-controls="panel">Материалы</button>
<section id="panel" role="tabpanel">Начальная вкладка</section>
<script>
async function load(value){
 const rows=document.getElementById('rows'); rows.setAttribute('aria-busy','true');
 const data=await(await fetch('/native-data?value='+encodeURIComponent(value))).json();
 rows.innerHTML='<article class="lesson">'+data.title+'</article>';
 rows.setAttribute('aria-busy','false');
}
document.getElementById('from').onchange=e=>load(e.target.value);
document.getElementById('kind').onchange=e=>load(e.target.value);
document.getElementById('tab').onclick=()=>document.getElementById('panel').textContent='Свежая вкладка';
</script></main>"""


@contextlib.asynccontextmanager
async def environment(
    gateway: PlaywrightGateway, html: str = HTML
) -> AsyncIterator[tuple[PlaywrightHandle, ReadCapabilityRegistry]]:
    handle = await gateway.open(
        user_id=new_id(),
        connection_id=new_id(),
        allow_hosts=("fakeuni.test", "id.fakeuni.test"),
        read_only=True,
        background=True,
    )
    registry = ReadCapabilityRegistry(handle.page, now=lambda: NOW)

    async def page(route: Route) -> None:
        if route.request.method != "GET":
            await route.fallback()
            return
        await route.fulfill(content_type="text/html; charset=utf-8", body=html)

    async def data(route: Route) -> None:
        if route.request.method != "GET":
            await route.fallback()
            return
        values = parse_qs(urlsplit(route.request.url).query)
        import json

        await route.fulfill(
            content_type="application/json",
            body=json.dumps({"title": "Данные: " + values.get("value", [""])[0]}),
        )

    await handle.context.route(URL, page)
    await handle.context.route(ORIGIN + "/native-data**", data)
    await handle.goto(URL)
    try:
        yield handle, registry
    finally:
        await registry.aclose()
        await handle.close()


async def one(registry: ReadCapabilityRegistry, kind: str) -> ReadCapability:
    found = [capability for capability in await registry.discover() if capability.kind == kind]
    assert len(found) == 1
    return found[0]


async def test_native_date_select_and_tab_compile_only_current_observed_choices(
    gateway: PlaywrightGateway,
) -> None:
    async with environment(gateway) as (_, registry):
        date = await one(registry, "set_date")
        assert [value.ref for value in date.values] == [
            "today",
            "weekStart",
            "previousWeek",
            "nextWeek",
            "twoWeeks",
        ]
        result = await registry.apply(date.ref, value_ref="today")
        assert "Данные: 2026-10-29" in result.snapshot.text
        assert result.start_url == URL
        assert result.plan.steps[0].value == "{today}"
        assert result.plan.steps[0].target.selector == "#from"
        select = await one(registry, "select_option")
        lectures = [value for value in select.values if value.label == "Лекции"]
        assert len(lectures) == 1
        selected = await registry.apply(select.ref, value_ref=lectures[0].ref)
        assert "Данные: lectures" in selected.snapshot.text
        assert [step.op for step in selected.plan.steps] == ["set_date", "select_option"]
        tab = await one(registry, "activate_tab")
        opened = await registry.apply(tab.ref)
        assert "Свежая вкладка" in opened.snapshot.text


async def test_plan_replays_on_fresh_browser_with_fresh_date_binding(
    gateway: PlaywrightGateway,
) -> None:
    async with environment(gateway) as (_, registry):
        date = await one(registry, "set_date")
        compiled = await registry.apply(date.ref, value_ref="today")
    async with environment(gateway) as (fresh, replay):
        snapshots = await replay.replay(
            compiled.plan, values={"{today}": "2026-11-02"}, items_selector=".lesson"
        )
        assert len(snapshots) == 1
        assert "Данные: 2026-11-02" in snapshots[0].text
        assert await fresh.page.input_value("#from") == "2026-11-02"


async def test_body_completion_proof_does_not_create_response_finished_close_waiters(
    gateway: PlaywrightGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden_finished(response: Response) -> None:
        raise AssertionError("registry must use requestfinished body completion events")

    monkeypatch.setattr(Response, "finished", forbidden_finished)
    async with environment(gateway) as (_, registry):
        date = await one(registry, "set_date")
        result = await registry.apply(date.ref, value_ref="today")
        assert "Данные: 2026-10-29" in result.snapshot.text


async def test_preloaded_hidden_tab_panel_becomes_visible_and_replays(
    gateway: PlaywrightGateway,
) -> None:
    html = HTML.replace(
        '<section id="panel" role="tabpanel">',
        '<section id="panel" role="tabpanel" style="display:none">',
    ).replace(
        "document.getElementById('panel').textContent='Свежая вкладка'",
        "document.getElementById('panel').style.display='block'",
    )
    async with environment(gateway, html) as (_, registry):
        tab = await one(registry, "activate_tab")
        result = await registry.apply(tab.ref)
        assert result.plan.steps[0].op == "activate_tab"
    async with environment(gateway, html) as (handle, registry):
        await registry.replay(result.plan, values={}, items_selector=".lesson")
        assert await handle.page.locator("#panel").is_visible()


@pytest.mark.parametrize("change", ["replace", "relabel", "form"])
async def test_stale_dom_reference_is_not_retargeted(
    gateway: PlaywrightGateway, change: str
) -> None:
    async with environment(gateway) as (handle, registry):
        date = await one(registry, "set_date")
        scripts = {
            "replace": "() => {const e=document.getElementById('from');e.replaceWith(e.cloneNode(true));}",
            "relabel": "() => document.getElementById('from').setAttribute('aria-label','Другой фильтр')",
            "form": "() => {const f=document.createElement('form');const e=document.getElementById('from');e.before(f);f.append(e);}",
        }
        await handle.page.evaluate(scripts[change])
        with pytest.raises(ReadEffectError, match="stale_reference"):
            await registry.apply(date.ref, value_ref="today")


async def test_registry_rejects_arbitrary_selector_value_and_mutating_controls(
    gateway: PlaywrightGateway,
) -> None:
    unsafe = HTML.replace(
        "<main>",
        """<main><form><label>Дата<input type="date" aria-controls="rows"></label></form>
    <input role="combobox" aria-controls="rows" aria-label="Календарь">
    <button type="submit" role="tab" aria-controls="panel">Отправить</button>
    <a rel="next" href="https://other.invalid/">Следующая</a>""",
    )
    async with environment(gateway, unsafe) as (_, registry):
        found = await registry.discover()
        assert sum(cap.kind == "set_date" for cap in found) == 1
        assert not any(cap.kind == "next_link" for cap in found)
        date = next(cap for cap in found if cap.kind == "set_date")
        with pytest.raises(ReadEffectError, match="stale_reference"):
            await registry.apply("#from", value_ref="today")
        with pytest.raises(ReadEffectError, match="unknown_value"):
            await registry.apply(date.ref, value_ref="2026-12-31")


@pytest.mark.parametrize("proof", ["dom_only", "get_only", "wrong_region", "busy"])
async def test_filter_requires_both_new_get_and_owned_region_change(
    gateway: PlaywrightGateway, monkeypatch: pytest.MonkeyPatch, proof: str
) -> None:
    monkeypatch.setattr(capabilities_module, "WAIT_SECONDS", 0.5)
    monkeypatch.setattr(capabilities_module, "QUIET_SECONDS", 0.05)
    actions = {
        "dom_only": "document.getElementById('rows').textContent='Без сетевого доказательства'",
        "get_only": "fetch('/native-data?value=only-get')",
        "wrong_region": "fetch('/native-data?value=other').then(()=>document.getElementById('panel').textContent='Другая область')",
        "busy": "document.getElementById('rows').setAttribute('aria-busy','true');fetch('/native-data?value=busy').then(()=>document.getElementById('rows').textContent='Ещё загружается')",
    }
    html = HTML.replace("e=>load(e.target.value)", "e=>{" + actions[proof] + "}", 1)
    async with environment(gateway, html) as (_, registry):
        date = await one(registry, "set_date")
        with pytest.raises(ReadEffectError, match="no_progress"):
            await registry.apply(date.ref, value_ref="today")


async def test_native_filter_does_not_open_post_or_websocket_permissions(
    gateway: PlaywrightGateway,
) -> None:
    html = HTML.replace(
        "e=>load(e.target.value)",
        """e=>{load(e.target.value);fetch('/write',{method:'POST'}).catch(()=>document.body.dataset.write='blocked');const ws=new WebSocket('wss://fakeuni.test/native-socket');ws.onclose=()=>document.body.dataset.ws='blocked';}""",
        1,
    )
    async with environment(gateway, html) as (handle, registry):
        date = await one(registry, "set_date")
        result = await registry.apply(date.ref, value_ref="today")
        assert "Данные: 2026-10-29" in result.snapshot.text
        await handle.page.wait_for_function(
            "document.body.dataset.write==='blocked' && document.body.dataset.ws==='blocked'"
        )


async def test_final_leaf_waits_for_data_after_parent_container(gateway: PlaywrightGateway) -> None:
    html = '<main><div class="day"></div></main>'
    async with environment(gateway, html) as (handle, registry):
        await handle.page.evaluate(
            """() => setTimeout(async()=>{await fetch('/native-data?value=late');setTimeout(()=>document.querySelector('.day').innerHTML='<article class="lesson">Поздняя пара</article>',100);},100)"""
        )
        assert await handle.page.locator(".lesson").count() == 0
        pages = await registry.replay(BrowserPlan(), values={}, items_selector=".day .lesson")
        assert "Поздняя пара" in pages[0].text


async def test_empty_requires_explicit_current_marker(
    gateway: PlaywrightGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capabilities_module, "WAIT_SECONDS", 0.5)
    monkeypatch.setattr(capabilities_module, "QUIET_SECONDS", 0.05)
    target = ReadTarget(selector="#empty", tag="p", role="status", label="Нет занятий")
    async with environment(gateway, '<main><p id="empty" role="status">Нет занятий</p></main>') as (
        _,
        registry,
    ):
        pages = await registry.replay(
            BrowserPlan(emptyTarget=target), values={}, items_selector=".lesson"
        )
        assert len(pages) == 1
    async with environment(gateway, '<main><p id="empty" role="status">Загрузка</p></main>') as (
        _,
        registry,
    ):
        with pytest.raises(ReadEffectError, match="no_progress"):
            await registry.replay(
                BrowserPlan(emptyTarget=target), values={}, items_selector=".lesson"
            )


async def test_same_origin_frame_replays_and_cross_origin_frame_is_not_a_capability(
    gateway: PlaywrightGateway,
) -> None:
    html = '<main><iframe id="course" title="Учебная область" src="/native-frame"></iframe><iframe title="Чужая область" src="https://id.fakeuni.test/native-frame"></iframe></main>'
    async with environment(gateway, html) as (handle, registry):

        async def frame(route: Route) -> None:
            await route.fulfill(
                content_type="text/html; charset=utf-8",
                body='<main><article class="lesson">Встроенная пара</article></main>',
            )

        await handle.context.route("**/native-frame", frame)
        await handle.goto(URL)
        frames = [cap for cap in await registry.discover() if cap.kind == "frame"]
        assert len(frames) == 1
        result = await registry.apply(frames[0].ref)
        assert "Встроенная пара" in result.snapshot.text
        assert result.plan.frame_path == [ReadFrame(selector="#course", title="Учебная область")]
        pages = await registry.replay(result.plan, values={}, items_selector=".lesson")
        assert len(pages) == 1


@pytest.mark.parametrize("mode", ["complete", "limit", "cycle"])
async def test_next_link_pagination_never_certifies_a_limit_or_cycle_as_complete(
    gateway: PlaywrightGateway, mode: str
) -> None:
    async with environment(gateway) as (handle, registry):

        async def page(route: Route) -> None:
            index = int(parse_qs(urlsplit(route.request.url).query).get("page", ["1"])[0])
            value = 1 if mode == "cycle" else index
            link = (
                f'<a id="next" rel="next" href="/native-pages?page={index + 1}">Следующая</a>'
                if mode != "complete" or index < 2
                else ""
            )
            await route.fulfill(
                content_type="text/html; charset=utf-8",
                body=f'<main><article class="lesson">Пара {value}</article>{link}</main>',
            )

        await handle.context.route(ORIGIN + "/native-pages**", page)
        await handle.goto(ORIGIN + "/native-pages?page=1")
        registry.reset()
        next_cap = await one(registry, "next_link")
        compiled = await registry.apply(next_cap.ref)
        assert compiled.plan.pagination is not None
        await handle.goto(ORIGIN + "/native-pages?page=1")
        registry.reset()
        if mode == "complete":
            pages = await registry.replay(compiled.plan, values={}, items_selector=".lesson")
            assert len(pages) == 2
        else:
            with pytest.raises(
                ReadEffectError, match="limit" if mode == "limit" else "no_progress"
            ):
                await registry.replay(compiled.plan, values={}, items_selector=".lesson")
