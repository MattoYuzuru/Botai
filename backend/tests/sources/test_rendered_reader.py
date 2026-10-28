"""Read-only reader budgets, cleanup and authentication boundaries without external traffic."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from botai.core.browser import FakeBrowserGateway, FakeBrowserHandle, FakePage, PageSnapshot
from botai.core.errors import BotaiError, ErrorCode
from botai.sources import rendered
from botai.sources.adapter import SessionExpired
from botai.sources.rendered import RenderedPageReader
from tests.sources.helpers import NOW, make_ctx

URL = "https://fakeuni.test/schedule"


class RecordingGateway(FakeBrowserGateway):
    async def open(self, **kwargs: Any) -> FakeBrowserHandle:
        assert kwargs["read_only"] is True
        assert kwargs["background"] is True
        return await super().open(**kwargs)


async def test_reader_shares_three_page_budget_and_closes_only_its_handles() -> None:
    browser = RecordingGateway(
        {URL: FakePage('<article class="event">Одна пара</article>')}, NOW, "https://fakeuni.test"
    )
    ctx = make_ctx(transport=httpx.MockTransport(lambda _: httpx.Response(200)), logged_in=False)
    ctx.browser = browser
    reader = RenderedPageReader(ctx)
    for _ in range(3):
        assert "Одна пара" in (await reader.read(URL, "article.event")).html
    with pytest.raises(BotaiError, match="source_rate_limited") as error:
        await reader.read(URL, "article.event")
    assert error.value.code == ErrorCode.SOURCE_RATE_LIMITED
    assert len(browser.handles) == 3
    assert all(handle.closed for handle in browser.handles)


async def test_reader_timeout_closes_acquired_background_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowGateway(RecordingGateway):
        async def open(self, **kwargs: Any) -> FakeBrowserHandle:
            handle = await super().open(**kwargs)

            async def slow_goto(url: str) -> PageSnapshot:
                await asyncio.sleep(10)
                return PageSnapshot(url=url)

            monkeypatch.setattr(handle, "goto", slow_goto)
            return handle

    browser = SlowGateway({}, NOW, "https://fakeuni.test")
    ctx = make_ctx(transport=httpx.MockTransport(lambda _: httpx.Response(200)), logged_in=False)
    ctx.browser = browser
    monkeypatch.setattr(rendered, "MAX_RENDERED_SECONDS", 1.05)
    with pytest.raises(BotaiError, match="source_unavailable"):
        await RenderedPageReader(ctx).read(URL, "article.event")
    assert len(browser.handles) == 1
    assert browser.handles[0].closed


@pytest.mark.parametrize(
    "page",
    [
        PageSnapshot(url="https://id.fakeuni.test/login"),
        PageSnapshot(url=URL, html='<form><input type="password"></form>'),
        PageSnapshot(
            url=URL,
            html="<main><h2>Вы не вошли в аккаунт</h2><p>Для продолжения войдите</p></main>",
        ),
        PageSnapshot(url=URL, text="You are not signed in"),
    ],
)
async def test_reader_auth_transition_requires_normal_login(page: PageSnapshot) -> None:
    ctx = make_ctx(transport=httpx.MockTransport(lambda _: httpx.Response(200)), logged_in=False)
    reader = RenderedPageReader(ctx)
    with pytest.raises(SessionExpired, match="login"):
        reader._check_page(page)


async def test_reader_browser_error_is_transient_and_captcha_is_human_work() -> None:
    ctx = make_ctx(transport=httpx.MockTransport(lambda _: httpx.Response(200)), logged_in=False)
    reader = RenderedPageReader(ctx)
    with pytest.raises(BotaiError, match="source_unavailable"):
        reader._check_page(PageSnapshot(url="chrome-error://chromewebdata"))
    with pytest.raises(BotaiError, match="source_bot_protection"):
        reader._check_page(PageSnapshot(url=URL, captcha_suspected=True))


async def test_reader_fails_closed_without_browser() -> None:
    ctx = make_ctx(transport=httpx.MockTransport(lambda _: httpx.Response(200)), logged_in=False)
    with pytest.raises(BotaiError, match="recipe_rejected"):
        await RenderedPageReader(ctx).read(URL, "article.event")
