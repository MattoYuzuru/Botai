"""The link between the API process and the worker that holds the browser (browser.screen_remote).

The WebSocket behaviour through the link is covered by every test in `test_screen.py`, which runs
in both topologies; these are the link's own rules.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from botai.browser.screen import (
    ScreenError,
    ScreenRegistry,
    _internal_bind,
    _remote_urls,
    screen_secret,
)
from botai.browser.screen_remote import AUTH_HEADER, sign
from botai.core.ids import new_id
from botai.core.vault import generate_kek_b64
from botai.settings import Settings
from tests.browser.test_screen import (
    PUBLIC,
    SECRET,
    ScreenRig,
    make_relayed_screen,  # noqa: F401 - fixtures
    make_screen,  # noqa: F401
)

pytestmark = pytest.mark.browser


def _worker_url(rig: ScreenRig, path: str) -> str:
    assert rig.worker is not None
    return f"ws://127.0.0.1:{rig.worker.internal_port}{path}"


def _signed(rig: ScreenRig, path: str, *, at: float | None = None) -> dict[str, str]:
    assert rig.worker is not None
    return {AUTH_HEADER: sign(rig.worker._link_key, path, now=at)}


async def test_the_link_serves_only_signed_requests(
    make_relayed_screen: Callable[..., Awaitable[ScreenRig]],  # noqa: F811
) -> None:
    rig = await make_relayed_screen()
    find = f"/internal/screens/find?user={rig.user}&connection={rig.conn}"
    async with connect(_worker_url(rig, find), additional_headers=_signed(rig, find)) as ws:
        assert json.loads(await ws.recv()) == {"sessionId": rig.session.id}
    for headers in (
        {},
        {AUTH_HEADER: "garbage"},
        {AUTH_HEADER: "1.AAAA"},
        _signed(rig, find, at=time.time() - 120),  # replayed after the skew window
        _signed(rig, "/internal/screens/find?user=x&connection=y"),  # signed for another request
    ):
        with pytest.raises(InvalidStatus) as exc:
            await connect(_worker_url(rig, find), additional_headers=headers)
        assert exc.value.response.status_code == 403


async def test_the_link_knows_whose_screen_it_is(
    make_relayed_screen: Callable[..., Awaitable[ScreenRig]],  # noqa: F811
) -> None:
    rig = await make_relayed_screen()
    other = new_id()
    find = f"/internal/screens/find?user={other}&connection={rig.conn}"
    async with connect(_worker_url(rig, find), additional_headers=_signed(rig, find)) as ws:
        assert json.loads(await ws.recv()) == {"sessionId": None}
    for path in (
        f"/internal/screens/{rig.session.id}/ws?user={other}",  # somebody else's screen
        f"/internal/screens/{'X' * 26}/ws?user={rig.user}",  # no such screen
        "/internal/other",  # no such endpoint
    ):
        with pytest.raises(InvalidStatus) as exc:
            await connect(_worker_url(rig, path), additional_headers=_signed(rig, path))
        assert exc.value.response.status_code == 404


async def test_an_unreachable_worker_means_no_screen_not_an_error(
    make_screen: Callable[..., Awaitable[ScreenRig]],  # noqa: F811
) -> None:
    rig = await make_screen()
    lonely = ScreenRegistry(
        secret=SECRET, public_base_url=PUBLIC, remote_urls=["http://127.0.0.1:9"]
    )
    started = time.perf_counter()
    assert await lonely.lookup(rig.user, rig.conn) is None
    with pytest.raises(ScreenError) as exc:
        await lonely.connect_remote(rig.session.id, rig.user)
    assert exc.value.code == 4404
    assert time.perf_counter() - started < 3


async def test_a_valid_ticket_for_a_screen_nobody_holds_is_refused_at_the_api(
    make_relayed_screen: Callable[..., Awaitable[ScreenRig]],  # noqa: F811
) -> None:
    rig = await make_relayed_screen()
    ghost = "G" * 26
    ticket = rig.registry.mint_ticket_for(ghost, rig.user).ticket
    with pytest.raises(InvalidStatus) as exc:
        await rig.connect(ticket=ticket, session_id=ghost)
    assert exc.value.response.status_code == 403


async def test_lookup_prefers_the_local_screen(
    make_screen: Callable[..., Awaitable[ScreenRig]],  # noqa: F811
) -> None:
    rig = await make_screen()
    assert await (rig.worker or rig.registry).lookup(rig.user, rig.conn) == rig.session.id
    assert await rig.registry.lookup(rig.user, rig.conn) == rig.session.id
    assert await rig.registry.lookup(new_id(), rig.conn) is None


def test_link_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BOTAI_SCREEN_BIND", "BOTAI_SCREEN_INTERNAL_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("botai.browser.screen.in_container", lambda: False)
    assert _internal_bind() == ("127.0.0.1", 8765)
    assert _remote_urls() == ["http://127.0.0.1:8765", "http://worker:8765"]
    monkeypatch.setattr("botai.browser.screen.in_container", lambda: True)
    assert _internal_bind() == ("0.0.0.0", 8765)  # noqa: S104 - the Docker network
    monkeypatch.setenv("BOTAI_SCREEN_BIND", "10.0.0.5:9000")
    monkeypatch.setenv("BOTAI_SCREEN_INTERNAL_URL", "http://a:1, http://b:2")
    assert _internal_bind() == ("10.0.0.5", 9000)
    assert _remote_urls() == ["http://a:1", "http://b:2"]
    monkeypatch.setenv("BOTAI_SCREEN_BIND", "off")
    monkeypatch.setenv("BOTAI_SCREEN_INTERNAL_URL", "off")
    assert _internal_bind() is None
    assert _remote_urls() == []


def test_ticket_key_is_shared_by_api_and_worker_through_the_vault_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOTAI_SCREEN_SECRET", raising=False)
    kek = generate_kek_b64()

    def secret(**kw: object) -> bytes:
        return screen_secret(Settings(_env_file=None, **kw))  # type: ignore[arg-type]

    from_vault_key = secret(botai_kek=kek, env="prod")
    assert from_vault_key == secret(botai_kek=kek, env="prod")  # api and worker agree
    assert from_vault_key != secret(botai_kek=generate_kek_b64(), env="prod")
    with pytest.raises(RuntimeError, match="BOTAI_SCREEN_SECRET"):
        secret(env="prod")
    assert secret(env="dev")  # a public constant, never accepted in prod
    monkeypatch.setenv("BOTAI_SCREEN_SECRET", "x" * 40)
    assert secret(botai_kek=kek, env="prod") != from_vault_key  # an explicit secret wins
