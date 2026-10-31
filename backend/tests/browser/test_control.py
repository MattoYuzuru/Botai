"""Full browser control on a real Chromium against a local HTTP site (marker `browser`).

Covers what `agent/tools/browser.py` needs from `browser/control.py`: navigation to an IP
literal and through a redirect, click/type/press/select targets, reading text/DOM/links/tables/
ARIA, JavaScript evaluation, screenshots, cookies and storage, storage-state export/import,
network log and HAR, downloads and uploads, several tabs and contexts, isolation, and closing.

The site is a plain loopback ASGI app (`tests/browser/local_site.py`): no EgressProxy, no TLS.
The browser is the gateway's shared full-control Chromium — the same one v4 will run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from botai.browser.control import (
    BrowserControl,
    ControlConfig,
    NetworkEntry,
    Target,
    mask_primary_secrets,
)
from botai.browser.gateway import PlaywrightGateway
from botai.core.errors import BotaiError, ErrorCode
from tests.browser.local_site import (
    COOKIE_VALUE,
    LOCAL_STORAGE_VALUE,
    SECRET_PAGE_MARKER,
    SESSION_STORAGE_VALUE,
    LocalSite,
)

pytestmark = pytest.mark.browser


@pytest.fixture
async def control(gateway: PlaywrightGateway, tmp_path: Path) -> AsyncIterator[BrowserControl]:
    browser = await BrowserControl.launch(
        gateway=gateway,
        config=ControlConfig(
            sandbox=False,
            capture_network_bodies=True,
            upload_root=tmp_path,
        ),
    )
    try:
        yield browser
    finally:
        await browser.close()


async def _wait_request(
    control: BrowserControl, needle: str, *, body: bool = False
) -> NetworkEntry:
    for _ in range(150):
        for entry in reversed(control.network_log()):
            if needle in entry.url and (not body or entry.request_body):
                return entry
        await asyncio.sleep(0.02)
    raise AssertionError(f"request {needle} was not captured")


# ------------------------------------------------------------------------------------ basics


async def test_navigates_to_an_ip_literal_reads_and_evaluates(
    control: BrowserControl, local_site: LocalSite
) -> None:
    info = await control.navigate(local_site.url("/"))
    assert info.url == local_site.url("/")  # an IP literal, not a host from any allowlist
    assert info.title == "Control Lab"
    assert "Проверка браузера" in await control.read_text()
    assert 'id="btn"' in await control.read_dom()
    links = await control.read_links()
    assert any(link["href"].endswith("/page2") for link in links)
    tables = await control.read_tables()
    assert tables
    assert tables[0][0] == ["Предмет", "Оценка"]
    aria = await control.read_aria()
    assert "Проверка браузера" in aria
    assert await control.eval_js("() => document.title") == "Control Lab"
    assert await control.eval_js("(arg) => arg * 2", arg=21) == 42
    assert (await control.read("links")).startswith("[")


async def test_redirect_is_followed(control: BrowserControl, local_site: LocalSite) -> None:
    info = await control.navigate(local_site.url("/redirect"))
    assert info.url == local_site.url("/page2")
    assert "вторая" in await control.read_text()


async def test_click_type_press_and_select(control: BrowserControl, local_site: LocalSite) -> None:
    await control.navigate(local_site.url("/"))
    await control.click({"selector": "#btn"})
    assert await control.eval_js('() => document.querySelector("#out").textContent') == "нажато"
    await control.click({"text": "Нажми меня"})
    await control.click({"role": "button", "name": "Нажми меня"})
    await control.click({"x": 20, "y": 20})
    assert (await control.page_info()).url == local_site.url("/")

    await control.navigate(local_site.url("/"))
    await control.select({"selector": "#pick"}, label="Второй")
    assert await control.eval_js('() => document.querySelector("#pick").value') == "b"

    await control.navigate(local_site.url("/form"))
    await control.type({"selector": "#name"}, "Алиса")
    await control.type({"label": "Пароль"}, "s3cret-pass")
    info = await control.click({"selector": "#submit"})
    assert "Результат: Алиса" in await control.read_text()
    assert "/echo" in info.url

    await control.navigate(local_site.url("/enter"))
    await control.type({"selector": "#q"}, "Боб")
    await control.press({"selector": "#q"}, "Enter")
    assert "Результат: Боб" in await control.read_text()

    await control.press(None, "Tab")
    await control.press(None, "e")


async def test_waits_for_a_delayed_element_and_times_out(
    control: BrowserControl, local_site: LocalSite
) -> None:
    await control.navigate(local_site.url("/slow"))
    await control.wait("selector", "#late", timeout_s=5.0)
    assert "поздно" in await control.read_text()
    await control.wait("timeout", 0.05)
    with pytest.raises(BotaiError) as exc:
        await control.wait("selector", "#never", timeout_s=0.2)
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_page_timeouts_are_not_reported_as_a_broken_source(
    control: BrowserControl,
) -> None:
    """A slow page/action is `browser_timeout` (504), not `source_unavailable` (site is down).

    Live L3 showed the model reading clicks/waits after SSO as a source problem; the two cases
    must stay distinguishable for it to retry the browser action instead of abandoning the source.
    """
    error = control._error(
        "Нажать", PlaywrightTimeout("Timeout 200ms exceeded"), url="https://lk.hse.ru/"
    )
    assert error.code == ErrorCode.BROWSER_TIMEOUT
    assert error.status == 504
    element = control._error(
        "Ждать", PlaywrightTimeout("Timeout 200ms exceeded"), target="button#login"
    )
    assert element.code == ErrorCode.NOT_FOUND


async def test_screenshot_png_jpeg_and_element(
    control: BrowserControl, local_site: LocalSite
) -> None:
    await control.navigate(local_site.url("/"))
    png = await control.screenshot()
    assert png.startswith(b"\x89PNG")
    jpeg = await control.screenshot(image_format="jpeg", quality=60)
    assert jpeg.startswith(b"\xff\xd8")
    element = await control.screenshot(target={"selector": "#greeting"})
    assert element.startswith(b"\x89PNG")


async def test_dialogs_are_dismissed_and_reported(
    control: BrowserControl, local_site: LocalSite
) -> None:
    await control.navigate(local_site.url("/dialog"))
    await control.click({"selector": "#alert"})
    assert any("привет из диалога" in message for message in control.dialogs())


# ------------------------------------------------------------------------- cookies and storage


async def test_cookies_and_storage_get_set_clear(
    control: BrowserControl, local_site: LocalSite
) -> None:
    await control.navigate(local_site.url("/js"))
    cookies = await control.cookies()
    assert any(
        cookie["name"] == "lab_cookie" and cookie["value"] == COOKIE_VALUE for cookie in cookies
    )
    assert (await control.get_storage("local")).get("token") == LOCAL_STORAGE_VALUE
    assert (await control.get_storage("session")).get("once") == SESSION_STORAGE_VALUE
    assert await control.set_storage({"extra": "value"}, "local") == 1
    assert (await control.get_storage("local"))["extra"] == "value"
    assert await control.set_storage({"temporary": "1"}, "session") == 1
    assert (await control.get_storage("session"))["temporary"] == "1"
    with pytest.raises(BotaiError) as exc:
        await control.get_storage("local", origin="http://example.test")
    assert exc.value.code == ErrorCode.VALIDATION

    await control.clear_cookies()
    assert await control.cookies() == []
    assert (
        await control.set_cookies(
            [{"name": "manual", "value": "v1", "domain": "127.0.0.1", "path": "/"}]
        )
        == 1
    )
    assert any(cookie["name"] == "manual" for cookie in await control.cookies())


async def test_storage_state_export_import_into_another_context(
    control: BrowserControl, local_site: LocalSite
) -> None:
    await control.navigate(local_site.url("/js"))
    state = await control.export_storage_state()
    assert any(cookie["name"] == "lab_cookie" for cookie in state["cookies"])
    assert state["origins"][0]["origin"] == local_site.base_url

    context = await control.new_context()
    control.switch_context(context.context_id)
    result = await control.import_storage_state(state)
    assert result.cookies_added >= 1
    assert result.origins_applied >= 1
    assert any(cookie["name"] == "lab_cookie" for cookie in await control.cookies())
    await control.navigate(local_site.url("/"))
    assert (await control.get_storage("local")).get("token") == LOCAL_STORAGE_VALUE


async def test_oversized_storage_state_is_rejected(
    control: BrowserControl, local_site: LocalSite
) -> None:
    await control.navigate(local_site.url("/"))
    huge = {
        "cookies": [
            {
                "name": f"c{index}",
                "value": "x" * 15_000,
                "domain": "127.0.0.1",
                "path": "/",
            }
            for index in range(200)
        ],
        "origins": [],
    }
    with pytest.raises(BotaiError) as exc:
        await control.import_storage_state(huge)
    assert exc.value.code == ErrorCode.VALIDATION
    assert "слишком большой" in exc.value.detail


# --------------------------------------------------------------------------- network and HAR


async def test_network_log_and_har_hold_request_and_response(
    control: BrowserControl, local_site: LocalSite
) -> None:
    control.clear_network_log()
    await control.navigate(local_site.url("/json"))
    payload = None
    for _ in range(100):
        entries = control.network_log()
        entry = next((item for item in entries if item.url.endswith("/json")), None)
        if entry is not None and entry.response_body:
            payload = entry
            break
        await asyncio.sleep(0.05)
    assert payload is not None, "the JSON response body was not captured"
    assert payload.status == 200
    assert json.loads(payload.response_body or "{}")["ok"] is True
    assert payload.request_headers  # working material for the agent, not for logs

    control.har_start()
    await control.navigate(local_site.url("/json?second=1"))
    har = control.har_stop()
    entries = har["log"]["entries"]
    assert any(entry["request"]["url"].endswith("/json?second=1") for entry in entries)
    json_entry = next(
        entry for entry in entries if entry["request"]["url"].endswith("/json?second=1")
    )
    assert json_entry["response"]["status"] == 200
    assert "ok" in json_entry["response"]["content"]["text"]
    with pytest.raises(BotaiError) as exc:
        control.har_stop()
    assert exc.value.code == ErrorCode.CONFLICT


# ------------------------------------------------------------------------------ download/upload


async def test_download_saves_a_private_file_and_hashes_it(
    control: BrowserControl, local_site: LocalSite, tmp_path: Path
) -> None:
    await control.navigate(local_site.url("/"))
    info = await control.download({"selector": "#dl"}, save_dir=tmp_path)
    saved = Path(info.path)
    assert saved.parent == tmp_path
    assert await asyncio.to_thread(saved.read_bytes) == b"payload-bytes"
    assert info.size_bytes == len(b"payload-bytes")
    assert info.sha256 == hashlib.sha256(b"payload-bytes").hexdigest()
    assert (await asyncio.to_thread(saved.stat)).st_mode & 0o077 == 0  # not readable by others


async def test_upload_from_bytes_and_from_disk(
    control: BrowserControl, local_site: LocalSite, tmp_path: Path
) -> None:
    await control.navigate(local_site.url("/"))
    assert await control.upload({"selector": "#file"}, [("note.txt", b"hello")]) == 1
    await control.click({"selector": "#send"})
    assert "uploaded:note.txt:5" in await control.read_text()

    source = tmp_path / "from-disk.csv"
    source.write_bytes(b"a,b\n1,2\n")
    await control.navigate(local_site.url("/"))
    await control.upload({"selector": "#file"}, [source])
    await control.click({"selector": "#send"})
    assert "uploaded:from-disk.csv:8" in await control.read_text()


async def test_upload_refuses_a_path_outside_the_workspace(
    control: BrowserControl, local_site: LocalSite, tmp_path: Path
) -> None:
    await control.navigate(local_site.url("/"))
    outside = tmp_path.parent / "outside-upload.txt"
    await asyncio.to_thread(outside.write_bytes, b"nope")
    try:
        with pytest.raises(BotaiError) as exc:
            await control.upload({"selector": "#file"}, [outside])
        assert exc.value.code == ErrorCode.FORBIDDEN
        assert "upload_root" in exc.value.detail
    finally:
        outside.unlink(missing_ok=True)

    # A bytes payload needs no directory at all.
    assert await control.upload({"selector": "#file"}, [("note.txt", b"ok")]) == 1


# ---------------------------------------------------- body redaction (primary secrets only)


def test_mask_primary_secrets_unit() -> None:
    masked = mask_primary_secrets(
        '{"login": "ivan", "password": "p@ss", "nested": {"otp": "123456"}}'
    )
    assert "p@ss" not in masked
    assert "123456" not in masked
    assert "ivan" in masked
    form = mask_primary_secrets("login=ivan&pass=p%40ss&name=Алиса")
    assert "p%40ss" not in form
    assert "login=ivan" in form
    assert "name=Алиса" in form


async def test_network_body_masks_password_fields_by_default(
    control: BrowserControl, local_site: LocalSite
) -> None:
    await control.navigate(local_site.url("/form"))
    await control.type({"selector": "#name"}, "Alice")
    await control.type({"selector": "#pass"}, "S3cret-POST-body")
    await control.click({"selector": "#submit"})
    entry = await _wait_request(control, "/echo", body=True)
    assert entry.request_body is not None
    assert "S3cret-POST-body" not in entry.request_body
    assert "pass=***" in entry.request_body
    assert "name=Alice" in entry.request_body  # not a primary secret: kept as working material


async def test_runtime_redactor_hook_masks_before_storage_and_har(
    gateway: PlaywrightGateway, local_site: LocalSite, tmp_path: Path
) -> None:
    def redact(text: str) -> str:
        return text.replace("Alice", "[name]")

    control = await BrowserControl.launch(
        gateway=gateway,
        config=ControlConfig(
            sandbox=False,
            capture_network_bodies=True,
            upload_root=tmp_path,
            redact_body=redact,
        ),
    )
    try:
        control.har_start()
        await control.navigate(local_site.url("/form"))
        await control.type({"selector": "#name"}, "Alice")
        await control.type({"selector": "#pass"}, "hook-pass-value")
        await control.click({"selector": "#submit"})
        entry = await _wait_request(control, "/echo", body=True)
        assert entry.request_body is not None
        assert "Alice" not in entry.request_body
        assert "[name]" in entry.request_body
        assert "hook-pass-value" not in entry.request_body
        har = control.har_stop()
        dumped = json.dumps(har, ensure_ascii=False)
        assert "Alice" not in dumped
        assert "hook-pass-value" not in dumped
    finally:
        await control.close()


# ---------------------------------------------------------------------------- tabs/contexts


async def test_tabs_are_isolated_switchable_and_closable(
    control: BrowserControl, local_site: LocalSite
) -> None:
    await control.navigate(local_site.url("/"))
    second = await control.new_tab(local_site.url("/page2"))
    tabs = await control.tabs()
    assert len(tabs) == 2
    assert second.tab_id == tabs[-1].tab_id
    assert "вторая" in await control.read_text()
    first = tabs[0]
    await control.switch_tab(first.tab_id)
    assert "Проверка браузера" in await control.read_text()
    await control.switch_tab(second.tab_id)
    await control.close_tab(second.tab_id)
    assert len(await control.tabs()) == 1


async def test_popup_becomes_a_tab(control: BrowserControl, local_site: LocalSite) -> None:
    await control.navigate(local_site.url("/popup"))
    await control.click({"selector": "#open"})
    for _ in range(50):
        tabs = await control.tabs()
        if len(tabs) >= 2:
            break
        await asyncio.sleep(0.05)
    assert len(await control.tabs()) == 2


async def test_contexts_are_isolated(control: BrowserControl, local_site: LocalSite) -> None:
    await control.navigate(local_site.url("/js"))
    first_context = control.contexts()[0]
    assert await control.cookies()

    created = await control.new_context()
    assert len(control.contexts()) == 2
    assert created.context_id != first_context.context_id
    control.switch_context(created.context_id)
    await control.navigate(local_site.url("/"))
    assert await control.cookies() == []  # a fresh context has its own jar
    await control.close_context(created.context_id)
    assert len(control.contexts()) == 1
    control.switch_context(first_context.context_id)
    assert await control.cookies()


# ----------------------------------------------------------------------------------- errors


async def test_bad_url_and_failed_navigation_are_clear(
    control: BrowserControl, local_site: LocalSite
) -> None:
    with pytest.raises(BotaiError) as exc:
        await control.navigate("file:///etc/passwd")
    assert exc.value.code == ErrorCode.VALIDATION
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(BotaiError) as exc:
        await control.navigate(f"http://127.0.0.1:{dead_port}/")  # nothing listens
    assert exc.value.code == ErrorCode.SOURCE_UNAVAILABLE
    assert "127.0.0.1" in exc.value.detail
    with pytest.raises(BotaiError) as exc:
        await control.click({"selector": "#missing"})
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_target_validation() -> None:
    with pytest.raises(BotaiError) as exc:
        Target.from_mapping({})
    assert exc.value.code == ErrorCode.VALIDATION
    with pytest.raises(BotaiError):
        Target.from_mapping({"selector": "#a", "text": "b"})
    with pytest.raises(BotaiError):
        Target.from_mapping({"x": 1})
    target = Target.from_mapping({"role": "button", "name": "Войти"})
    assert target.describe().startswith("role button")


async def test_close_is_idempotent_and_the_control_refuses_after(
    gateway: PlaywrightGateway,
) -> None:
    control = await BrowserControl.launch(gateway=gateway, config=ControlConfig(sandbox=False))
    await control.close()
    await control.close()
    with pytest.raises(BotaiError) as exc:
        await control.navigate("http://127.0.0.1/")
    assert exc.value.code == ErrorCode.CONFLICT


# ------------------------------------------------------------------------------- no leaks


async def test_page_content_cookies_and_typed_values_never_reach_logs(
    control: BrowserControl,
    local_site: LocalSite,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="botai.browser.control")
    caplog.set_level(logging.DEBUG, logger="botai.browser.gateway")
    await control.navigate(local_site.url("/secret"))
    await control.read_text()
    await control.navigate(local_site.url("/js"))
    await control.cookies()
    await control.export_storage_state()
    await control.navigate(local_site.url("/form"))
    typed = "P@ssw0rd-никому-не-показывать"
    await control.type({"selector": "#pass"}, typed)
    await control.read_dom()
    await control.screenshot()
    await control.navigate(local_site.url("/"))
    await control.download({"selector": "#dl"}, save_dir=tmp_path)
    text = caplog.text
    assert SECRET_PAGE_MARKER not in text
    assert COOKIE_VALUE not in text
    assert typed not in text
    assert LOCAL_STORAGE_VALUE not in text
