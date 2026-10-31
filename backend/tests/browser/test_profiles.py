"""Server-side persistent browser profiles (marker partly `browser`: real Chromium for the rest).

Filesystem tests need no browser: directory layout, 0700/0600 permissions, storage state
round-trip, cookie import, validation, listing, removal and the exclusive profile lock. The last
tests prove the point of the module with a real Chromium: a persistent user-data directory keeps
the cookie and localStorage across two launches, and a saved storage state is restored into a
fresh context.
"""

from __future__ import annotations

import asyncio
import logging
import stat
from pathlib import Path

import pytest

from botai.browser.control import BrowserControl, ControlConfig
from botai.browser.gateway import PlaywrightGateway
from botai.browser.profiles import (
    BrowserProfileStore,
    locked_profile,
    validate_cookie,
    validate_storage_state,
)
from botai.core.errors import BotaiError, ErrorCode
from tests.browser.local_site import (
    COOKIE_VALUE,
    LOCAL_STORAGE_VALUE,
    LocalSite,
)

pytestmark = pytest.mark.browser


@pytest.fixture
def store(tmp_path: Path) -> BrowserProfileStore:
    return BrowserProfileStore(tmp_path / "profiles")


def state(
    origin: str,
    *,
    cookie: str = "session-cookie-value",
    local: str = "local-storage-value",
) -> dict[str, object]:
    return {
        "cookies": [
            {
                "name": "session",
                "value": cookie,
                "domain": "127.0.0.1",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [{"origin": origin, "localStorage": [{"name": "token", "value": local}]}],
    }


# ---------------------------------------------------------------------------- children


def test_profile_directories_are_private(store: BrowserProfileStore) -> None:
    directory = store.profile_dir("user-1", "default")
    assert directory.is_dir()
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.root / "user-1").stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.user_data_dir("user-1").stat().st_mode) == 0o700


def test_existing_directory_mode_is_tightened(store: BrowserProfileStore) -> None:
    user_dir = store.root / "user-2"
    user_dir.mkdir(mode=0o755)
    directory = store.profile_dir("user-2", "work")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("name", ["../evil", "a/b", "", ".hidden", "плохое", "x" * 80])
def test_unsafe_profile_names_are_rejected(store: BrowserProfileStore, name: str) -> None:
    with pytest.raises(BotaiError) as exc:
        store.profile_dir("user-1", name)
    assert exc.value.code == ErrorCode.VALIDATION


def test_unsafe_user_id_is_rejected(store: BrowserProfileStore) -> None:
    with pytest.raises(BotaiError) as exc:
        store.profile_dir("../../etc", "default")
    assert exc.value.code == ErrorCode.VALIDATION


# ----------------------------------------------------------------------- storage state


def test_storage_state_roundtrip_is_private(store: BrowserProfileStore) -> None:
    saved = store.save_storage_state("user-1", "default", state("http://127.0.0.1:8000"))
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    loaded = store.load_storage_state("user-1", "default")
    assert loaded is not None
    assert loaded["cookies"][0]["name"] == "session"
    assert loaded["origins"][0]["localStorage"][0]["value"] == "local-storage-value"
    # No temp files are left next to the state after the atomic write.
    assert sorted(path.name for path in saved.parent.iterdir()) == ["storage_state.json"]


def test_missing_state_is_none(store: BrowserProfileStore) -> None:
    assert store.load_storage_state("nobody", "default") is None


def test_cookie_import_merges_and_replaces(store: BrowserProfileStore) -> None:
    origin = "http://127.0.0.1:8000"
    store.save_storage_state("user-1", "default", state(origin))
    first = store.import_cookies(
        "user-1",
        "default",
        [
            {"name": "session", "value": "replaced", "domain": "127.0.0.1", "path": "/"},
            {"name": "extra", "value": "new", "domain": "127.0.0.1", "path": "/"},
        ],
    )
    assert first == 1  # `session` replaced, `extra` added
    loaded = store.load_storage_state("user-1", "default")
    assert loaded is not None
    values = {cookie["name"]: cookie["value"] for cookie in loaded["cookies"]}
    assert values["session"] == "replaced"
    assert values["extra"] == "new"
    assert loaded["origins"]  # localStorage from the previous state survives


def test_clear_storage_state_keeps_chromium_data(store: BrowserProfileStore) -> None:
    store.save_storage_state("user-1", "default", state("http://127.0.0.1:8000"))
    assert store.clear_storage_state("user-1", "default") is True
    assert store.load_storage_state("user-1", "default") is None
    assert store.user_data_dir("user-1").is_dir()
    assert store.clear_storage_state("user-1", "default") is False


# -------------------------------------------------------------------------- validation


def test_invalid_storage_state_is_rejected() -> None:
    with pytest.raises(BotaiError):
        validate_storage_state("not a mapping")
    with pytest.raises(BotaiError) as exc:
        validate_storage_state({"cookies": "nope", "origins": []})
    assert exc.value.code == ErrorCode.VALIDATION
    with pytest.raises(BotaiError):
        validate_storage_state(
            {"cookies": [], "origins": [{"origin": "javascript:alert(1)", "localStorage": []}]}
        )
    with pytest.raises(BotaiError):
        validate_storage_state(
            {"cookies": [], "origins": [{"origin": "http://u:p@host.test", "localStorage": []}]}
        )
    with pytest.raises(BotaiError):
        validate_storage_state(
            {
                "cookies": [
                    {"name": f"c{index}", "value": "v", "domain": "host.test", "path": "/"}
                    for index in range(501)
                ],
                "origins": [],
            }
        )


def test_storage_state_over_the_size_limit_is_rejected(
    store: BrowserProfileStore,
) -> None:
    cookies = [
        {"name": f"c{index}", "value": "x" * 15_000, "domain": "host.test", "path": "/"}
        for index in range(200)  # about 3 MB serialised, over the 2 MB limit
    ]
    with pytest.raises(BotaiError) as exc:
        store.save_storage_state("user-1", "default", {"cookies": cookies, "origins": []})
    assert exc.value.code == ErrorCode.VALIDATION
    assert "слишком большой" in exc.value.detail
    assert not store.storage_state_path("user-1", "default").exists()


def test_invalid_cookie_is_rejected() -> None:
    with pytest.raises(BotaiError):
        validate_cookie({"name": "x"})
    with pytest.raises(BotaiError):
        validate_cookie({"value": "v", "domain": "host.test"})
    cookie = validate_cookie(
        {"name": "x", "value": "v", "domain": "host.test", "path": "/", "sameSite": "Lax"}
    )
    assert cookie["sameSite"] == "Lax"


# ------------------------------------------------------------------------- list and delete


def test_list_and_delete_profiles(store: BrowserProfileStore) -> None:
    store.save_storage_state("user-1", "alpha", state("http://127.0.0.1:8000"))
    store.user_data_dir("user-1", "beta")
    profiles = store.list("user-1")
    assert [profile.name for profile in profiles] == ["alpha", "beta"]
    alpha = next(profile for profile in profiles if profile.name == "alpha")
    assert alpha.has_storage_state
    assert alpha.has_user_data is False
    assert alpha.storage_state_bytes > 0
    assert profiles[1].has_storage_state is False
    assert profiles[1].has_user_data is True

    assert store.delete("user-1", "alpha") is True
    assert [profile.name for profile in store.list("user-1")] == ["beta"]
    assert store.delete("user-1", "alpha") is False
    assert store.list("unknown-user") == []


def test_default_root_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "custom-profiles"
    monkeypatch.setenv("BOTAI_BROWSER_PROFILES_DIR", str(target))
    store = BrowserProfileStore()
    assert store.root == target
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700


def test_lock_yields_the_profile_directory(store: BrowserProfileStore) -> None:
    async def run() -> Path:
        async with locked_profile(store, "user-1", "default") as directory:
            return directory

    assert asyncio.run(run()) == store.profile_dir("user-1", "default")


async def test_profile_lock_is_exclusive(store: BrowserProfileStore) -> None:
    async with store.lock("user-1", "default"):
        with pytest.raises(BotaiError) as exc:
            async with store.lock("user-1", "default", timeout_s=0.1):
                pass
        assert exc.value.code == ErrorCode.CONFLICT
    async with store.lock("user-1", "default", timeout_s=0.1):
        pass  # released properly and can be taken again


# ------------------------------------------------------------------------------ no leaks


def test_cookie_import_does_not_log_values(
    store: BrowserProfileStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    secret = "cookie-value-that-must-not-be-logged"
    store.import_cookies(
        "user-1",
        "default",
        [{"name": "session", "value": secret, "domain": "host.test", "path": "/"}],
    )
    assert secret not in caplog.text


# --------------------------------------------------------------- real Chromium over profiles


async def test_persistent_profile_keeps_cookies_and_local_storage(
    gateway: PlaywrightGateway,
    store: BrowserProfileStore,
    local_site: LocalSite,
) -> None:
    """The server-side persistent profile: the site sees the same storage after a relaunch."""
    directory = store.user_data_dir("user-1", "default")
    config = ControlConfig(sandbox=False)
    first = await BrowserControl.launch_persistent(
        directory, gateway=gateway, config=config, lock=store.lock("user-1", "default")
    )
    try:
        await first.navigate(local_site.url("/js"))
        cookies = await first.cookies()
        assert any(cookie["name"] == "lab_cookie" for cookie in cookies)
        storage = await first.get_storage("local")
        assert storage.get("token") == LOCAL_STORAGE_VALUE
    finally:
        await first.close()

    second = await BrowserControl.launch_persistent(
        directory, gateway=gateway, config=config, lock=store.lock("user-1", "default")
    )
    try:
        await second.navigate(local_site.url("/"))
        cookies = await second.cookies()
        assert any(
            cookie["name"] == "lab_cookie" and cookie["value"] == COOKIE_VALUE for cookie in cookies
        )
        storage = await second.get_storage("local")
        assert storage.get("token") == LOCAL_STORAGE_VALUE
    finally:
        await second.close()


async def test_saved_storage_state_restores_into_a_fresh_context(
    gateway: PlaywrightGateway,
    store: BrowserProfileStore,
    local_site: LocalSite,
) -> None:
    store.save_storage_state(
        "user-1",
        "default",
        state(local_site.base_url, cookie="restored-cookie", local="restored-storage"),
    )
    loaded = store.load_storage_state("user-1", "default")
    assert loaded is not None
    control = await BrowserControl.launch(
        gateway=gateway,
        config=ControlConfig(sandbox=False),
        storage_state=loaded,
    )
    try:
        await control.navigate(local_site.url("/"))
        cookies = await control.cookies()
        assert any(
            cookie["name"] == "session" and cookie["value"] == "restored-cookie"
            for cookie in cookies
        )
        storage = await control.get_storage("local")
        assert storage.get("token") == "restored-storage"
    finally:
        await control.close()


async def test_persistent_control_refuses_extra_contexts(
    gateway: PlaywrightGateway, store: BrowserProfileStore
) -> None:
    control = await BrowserControl.launch_persistent(
        store.user_data_dir("user-1", "default"),
        gateway=gateway,
        config=ControlConfig(sandbox=False),
        lock=store.lock("user-1", "default"),
    )
    try:
        assert control.contexts()[0].persistent is True
        with pytest.raises(BotaiError) as exc:
            await control.new_context()
        assert exc.value.code == ErrorCode.NOT_IMPLEMENTED
    finally:
        await control.close()


async def test_persistent_launch_requires_a_profile_lock(
    gateway: PlaywrightGateway, store: BrowserProfileStore
) -> None:
    with pytest.raises(BotaiError) as exc:
        await BrowserControl.launch_persistent(
            store.user_data_dir("user-1", "default"),
            gateway=gateway,
            config=ControlConfig(sandbox=False),
        )
    assert exc.value.code == ErrorCode.CONFLICT
    assert "lock" in exc.value.detail


async def test_a_second_persistent_launch_on_the_same_profile_is_a_conflict(
    gateway: PlaywrightGateway, store: BrowserProfileStore
) -> None:
    first = await BrowserControl.launch_persistent(
        store.user_data_dir("user-1", "default"),
        gateway=gateway,
        config=ControlConfig(sandbox=False),
        lock=store.lock("user-1", "default"),
    )
    try:
        with pytest.raises(BotaiError) as exc:
            await BrowserControl.launch_persistent(
                store.user_data_dir("user-1", "default"),
                gateway=gateway,
                config=ControlConfig(sandbox=False),
                lock=store.lock("user-1", "default", timeout_s=0.2),
            )
        assert exc.value.code == ErrorCode.CONFLICT
        assert "Профиль уже открыт" in exc.value.detail
    finally:
        await first.close()
