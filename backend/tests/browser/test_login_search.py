"""Post-login data forms must not become another authentication step."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Route

from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.browser.session import decode_session
from botai.core.vault import SecretKind
from tests.browser.test_login import (
    ORIGIN,
    Rig,
    Student,
    make_rig,  # noqa: F401 - fixture
    preset_dict,
    works,
)

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize(
    ("start", "kind", "name", "label", "expected"),
    [
        ("/plain/login", "text", "q", "Поиск курсов", ["login", "password"]),
        ("/auth/login", "search", "user_search", "Search courses", ["login", "password", "code"]),
        ("/two/username", "email", "user_search", "Поиск студента", ["login", "password"]),
    ],
)
async def test_post_login_search_is_never_asked_filled_or_submitted(
    make_rig: Callable[..., Rig],  # noqa: F811
    gateway: PlaywrightGateway,
    monkeypatch: pytest.MonkeyPatch,
    probe: bool,
    start: str,
    kind: str,
    name: str,
    label: str,
    expected: list[str],
) -> None:
    search_submits = 0
    original_open = gateway.open

    async def dashboard(route: Route) -> None:
        # Only the page markup changes. Authentication and cookies are provided by
        # the real fakeuni login/OTP handlers through the loopback HTTPS proxy.
        await route.fulfill(
            content_type="text/html",
            body=f"""<!doctype html><title>Учебные материалы</title>
<h1>Кабинет</h1><form id="course-search" action="/search" method="get">
<label for="q">{label}</label><input id="q" type="{kind}" name="{name}">
<button type="submit">Найти</button></form>""",
        )

    async def search(route: Route) -> None:
        nonlocal search_submits
        search_submits += 1
        await route.fulfill(content_type="text/html", body="<h1>Результаты поиска</h1>")

    async def open_with_search(**kwargs: Any) -> PlaywrightHandle:
        handle = await original_open(**kwargs)
        await handle.page.route(f"{ORIGIN}/app", dashboard)
        await handle.page.route(f"{ORIGIN}/search*", search)
        return handle

    monkeypatch.setattr(gateway, "open", open_with_search)
    if not probe:
        # Exercise the normal successful post-submit fallback when a generic
        # portal exposes no authenticated GET that can become a reusable probe.
        monkeypatch.setattr("botai.browser.login.learn_probe", AsyncMock(return_value=None))
    rig = make_rig()
    rig.store.seed_preset(ORIGIN, preset_dict(f"{ORIGIN}{start}", probe=probe))
    student = Student()
    session, ctx = await asyncio.wait_for(rig.ensure(student), timeout=15.0)
    assert await works(session)
    sealed = await rig.vault.get(rig.user, rig.conn, SecretKind.SESSION)
    assert sealed is not None
    assert await works(decode_session(sealed.get_secret_value()))
    assert [kind for kind, _ in student.asked] == expected
    assert all(
        "Поиск" not in question and "Search" not in question for question in student.questions
    )
    assert all("Проверьте" not in question for question in student.questions)
    assert search_submits == 0
    assert rig.flow._cooldown == {}
    assert rig.screens.links == []
    assert ctx.secrets_requested
