"""The agent's live read of a page (`agent_read`): it gives way to the student's other actions, it
never pushes one out, and its page cannot send an action (real Chromium, fakeuni)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from botai.browser.gateway import PlaywrightGateway, PlaywrightHandle
from botai.core.browser import BrowserBusy
from botai.core.ids import new_id
from tests.fakeuni import spa

pytestmark = pytest.mark.browser
HOSTS = ("fakeuni.test",)
QUIET = "https://fakeuni.test/spa-cabinet/quiet"


@pytest.fixture(autouse=True)
def fresh_state() -> None:
    spa.reset()


@pytest.fixture
async def opened(gateway: PlaywrightGateway) -> AsyncIterator[list[PlaywrightHandle]]:
    handles: list[PlaywrightHandle] = []
    yield handles
    for handle in handles:
        await handle.close()


async def open_for(
    gateway: PlaywrightGateway, handles: list[PlaywrightHandle], user: str, **kwargs: bool
) -> PlaywrightHandle:
    handle = await gateway.open(user_id=user, connection_id=new_id(), allow_hosts=HOSTS, **kwargs)
    handles.append(handle)
    return handle


AGENT = {"read_only": True, "background": True, "agent_read": True}
BACKGROUND = {"read_only": True, "background": True}


async def test_the_agents_read_gives_way_to_a_background_action_of_the_student(
    gateway: PlaywrightGateway, opened: list[PlaywrightHandle]
) -> None:
    user = new_id()
    reading = await open_for(gateway, opened, user, **AGENT)
    research = await open_for(gateway, opened, user, **BACKGROUND)  # a login check, a research
    assert reading.closed
    assert not research.closed
    assert gateway.active_for(user) is research


async def test_the_agents_read_never_pushes_out_a_running_action_or_another_read(
    gateway: PlaywrightGateway, opened: list[PlaywrightHandle]
) -> None:
    user = new_id()
    login = await open_for(gateway, opened, user)  # the student's own login window
    with pytest.raises(BrowserBusy):
        await open_for(gateway, opened, user, **AGENT)
    assert not login.closed
    await login.close()
    first = await open_for(gateway, opened, user, **AGENT)
    with pytest.raises(BrowserBusy):  # two reads of the agent do not push each other out
        await open_for(gateway, opened, user, **AGENT)
    assert not first.closed


async def test_a_background_action_still_does_not_push_out_a_running_one(
    gateway: PlaywrightGateway, opened: list[PlaywrightHandle]
) -> None:
    user = new_id()
    running = await open_for(gateway, opened, user, **BACKGROUND)
    with pytest.raises(BrowserBusy):
        await open_for(gateway, opened, user, **BACKGROUND)
    assert not running.closed


async def test_an_agents_read_is_read_only_and_in_the_background() -> None:
    gateway = PlaywrightGateway.__new__(PlaywrightGateway)  # the check comes before any browser
    with pytest.raises(ValueError, match="read-only background"):
        await gateway._open_for_user(  # pyright: ignore[reportPrivateUsage]
            user_id="u", connection_id="c", allow_hosts=HOSTS, agent_read=True
        )


FETCH_LOGOUT = "fetch('/spa-cabinet/api/logout').then(() => 'sent', () => 'blocked')"


async def test_the_page_of_an_agents_read_cannot_send_a_get_that_names_an_action(
    gateway: PlaywrightGateway, opened: list[PlaywrightHandle]
) -> None:
    reading = await open_for(gateway, opened, new_id(), **AGENT)
    await reading.goto(QUIET)
    assert await reading.page.evaluate(FETCH_LOGOUT) == "blocked"
    await reading.page.evaluate("location.assign('/spa-cabinet/api/logout')")
    await reading.page.wait_for_timeout(300)
    # the navigation was refused too: the frame is left on the browser's error page, which the
    # reader reports as a page that did not load, never as the page of the source
    assert reading.page.url.startswith("chrome-error:")
    assert spa.STATE.actions == 0


async def test_other_read_only_readers_are_not_changed_by_it(
    gateway: PlaywrightGateway, opened: list[PlaywrightHandle]
) -> None:
    """The refusal belongs to the agent's read; a recipe's or a login check's reader is as before."""
    other = await open_for(gateway, opened, new_id(), **BACKGROUND)
    await other.goto(QUIET)
    assert await other.page.evaluate(FETCH_LOGOUT) == "sent"
    assert spa.STATE.actions == 1
