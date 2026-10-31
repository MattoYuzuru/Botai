"""Browser harness of the live benchmark: the owner's real Chrome first, Playwright fallback.

Priority order (``mode="auto"``):

1. ``chrome-copy`` -- a real Google Chrome process started over CDP with a temporary copy of
   the owner's profile (``Local State`` + ``Default/Cookies`` + ``Preferences``). The profile
   is copied, never opened in place; the Chrome Keychain key stays inside Chrome, so cookie
   decryption is Chrome's own business and the benchmark never reads the master key. This mode
   keeps the device fingerprint, locale and time zone and exposes the **already authenticated**
   owner session (CU and HSE) without asking for a password.
2. ``chrome-fresh`` -- the same real Chrome binary with an empty temporary profile: a clean
   incognito context where the benchmark types the credentials from the secret broker and
   observes what the site actually does.
3. ``chromium`` -- bundled Playwright Chromium, only when Chrome cannot be launched.

All modes expose one API: :class:`BrowserSession` with ``new_context`` (HAR recorder,
injected cookies, storage state) and ``profile_cookies`` (session extraction in ``chrome-copy``).
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CHROME_CANDIDATES = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)
CHROME_USER_DATA = Path.home() / "Library/Application Support/Google/Chrome"
PROFILE_ITEMS = (
    "Local State",
    "Default/Cookies",
    "Default/Preferences",
    "Default/Local Storage",
)
PROFILE_SKIP = ("SingletonLock", "SingletonSocket", "SingletonCookie")
COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


class BrowserUnavailable(RuntimeError):
    """The requested browser mode cannot start on this machine."""


@dataclass(slots=True)
class BrowserConfig:
    mode: str = "auto"  # auto | chrome-copy | chrome-fresh | chromium
    headless: bool = True
    locale: str = "ru-RU"
    timezone_id: str = "Europe/Moscow"
    viewport: tuple[int, int] = (1440, 900)
    navigation_timeout_ms: int = 60_000
    cdp_start_timeout_s: float = 30.0

    @property
    def resolved_mode(self) -> str:
        if self.mode != "auto":
            return self.mode
        return "chrome-copy" if find_chrome() is not None else "chromium"


def find_chrome() -> Path | None:
    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_cdp(port: int, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            last_error = exc
            time.sleep(0.3)
    raise BrowserUnavailable(f"Chrome CDP endpoint did not start on port {port}: {last_error}")


def copy_chrome_profile(destination: Path) -> dict[str, Any]:
    """Copy the minimum of the owner's Chrome profile needed for the existing session.

    The live profile is only read. The copy lives under the run directory outside the repository.
    """
    if not CHROME_USER_DATA.exists():
        raise BrowserUnavailable(f"Chrome user data dir not found: {CHROME_USER_DATA}")
    (destination / "Default").mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for rel in PROFILE_ITEMS:
        source = CHROME_USER_DATA / rel
        if not source.exists():
            continue
        target = destination / rel
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        copied.append(rel)
    for name in PROFILE_SKIP:
        stale = destination / name
        if stale.is_symlink() or stale.exists():
            stale.unlink()
    return {"source": str(CHROME_USER_DATA), "items": copied, "profileDir": str(destination)}


def normalize_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the fields Playwright's ``add_cookies`` accepts."""
    normalized: list[dict[str, Any]] = []
    for cookie in cookies:
        item = {key: cookie[key] for key in COOKIE_KEYS if key in cookie}
        if "expires" in item and item["expires"] in (None, 0):
            item["expires"] = -1
        if "sameSite" in item and item["sameSite"] not in {"Strict", "Lax", "None"}:
            item.pop("sameSite")
        if item.get("name") and "domain" in item:
            normalized.append(item)
    return normalized


@dataclass(slots=True)
class BrowserSession:
    """One live browser and its contexts; close it to release the Chrome process."""

    mode: str
    headless: bool
    playwright: Any
    browser: Any = None
    profile_context: Any = None
    process: subprocess.Popen[bytes] | None = None
    profile_dir: Path | None = None
    chrome_version: str | None = None
    temp_root: Path | None = None
    _closed: bool = field(default=False, init=False)

    # --- contexts -----------------------------------------------------------------------------

    def new_context(
        self,
        *,
        har_path: Path | None = None,
        storage_state: dict[str, Any] | str | None = None,
        cookies: list[dict[str, Any]] | None = None,
        viewport: tuple[int, int] | None = None,
        timezone_id: str | None = None,
        locale: str | None = None,
    ) -> Any:
        """Create an isolated context (a real incognito context in Chrome mode)."""
        options: dict[str, Any] = {
            "locale": locale or "ru-RU",
            "timezone_id": timezone_id or "Europe/Moscow",
            "viewport": {
                "width": (viewport or (1440, 900))[0],
                "height": (viewport or (1440, 900))[1],
            },
            "ignore_https_errors": False,
        }
        if har_path is not None:
            har_path.parent.mkdir(parents=True, exist_ok=True)
            options.update(
                record_har_path=str(har_path),
                record_har_content="omit",
                record_har_mode="full",
            )
        if storage_state is not None:
            options["storage_state"] = storage_state
        if self.browser is None:
            raise BrowserUnavailable("browser is not started")
        context = self.browser.new_context(**options)
        if cookies:
            context.add_cookies(normalize_cookies(cookies))
        return context

    def profile_cookies(self) -> list[dict[str, Any]]:
        """Cookies of the copied Chrome profile (chrome-copy mode only). Never printed."""
        if self.profile_context is None:
            return []
        return list(self.profile_context.cookies())

    # --- lifecycle ----------------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        try:
            self.playwright.stop()
        except Exception:
            pass
        # The copied Chrome profile contains the owner's session databases; the run keeps the
        # extracted storage state, and the profile copy itself must not outlive the scenario.
        if self.profile_dir is not None and self.mode in {"chrome-copy", "chrome-fresh"}:
            shutil.rmtree(self.profile_dir, ignore_errors=True)


def _launch_chrome(
    profile_dir: Path,
    port: int,
    config: BrowserConfig,
) -> subprocess.Popen[bytes]:
    chrome = find_chrome()
    if chrome is None:
        raise BrowserUnavailable("Google Chrome is not installed")
    args = [
        str(chrome),
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--lang={config.locale.replace('-', '_')}",
        f"--window-size={config.viewport[0]},{config.viewport[1]}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
    ]
    if config.headless:
        args.append("--headless=new")
    else:
        args.extend(["--window-position=-2200,0", "--window-size=1440,900"])
    args.append("about:blank")
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def start_browser(config: BrowserConfig, temp_root: Path) -> BrowserSession:
    """Start the browser according to ``config`` and connect Playwright to it."""
    from playwright.sync_api import sync_playwright  # local import: broker must work without it

    mode = config.resolved_mode
    playwright = sync_playwright().start()
    session = BrowserSession(
        mode=mode,
        headless=config.headless,
        playwright=playwright,
        temp_root=temp_root,
    )
    try:
        if mode == "chromium":
            session.browser = playwright.chromium.launch(
                headless=config.headless,
                args=[f"--lang={config.locale.replace('-', '_')}"],
            )
            return session
        if mode not in {"chrome-copy", "chrome-fresh"}:
            raise BrowserUnavailable(f"unknown browser mode {mode!r}")
        profile_dir = temp_root / (
            "chrome-profile-copy" if mode == "chrome-copy" else "chrome-profile-fresh"
        )
        if mode == "chrome-copy":
            copy_chrome_profile(profile_dir)
        else:
            profile_dir.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        session.process = _launch_chrome(profile_dir, port, config)
        version_info = _wait_for_cdp(port, config.cdp_start_timeout_s)
        session.chrome_version = str(version_info.get("Browser") or "Chrome/unknown")
        session.profile_dir = profile_dir
        session.browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        contexts = session.browser.contexts
        session.profile_context = contexts[0] if contexts else None
        return session
    except Exception:
        session.close()
        raise


def open_browser(config: BrowserConfig, temp_root: Path) -> Iterator[BrowserSession]:
    """Context manager wrapper around :func:`start_browser`."""
    session = start_browser(config, temp_root)
    try:
        yield session
    finally:
        session.close()


def cookie_summary(cookies: list[dict[str, Any]], suffixes: tuple[str, ...]) -> dict[str, Any]:
    """Describe cookies by domain without exposing values."""
    now = time.time()
    summary: dict[str, Any] = {
        "total": len(cookies),
        "domains": {},
    }
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if not any(domain.endswith(suffix) for suffix in suffixes):
            continue
        bucket = summary["domains"].setdefault(domain, {"count": 0, "names": [], "expired": 0})
        bucket["count"] += 1
        name = str(cookie.get("name") or "")
        if name and name not in bucket["names"]:
            bucket["names"].append(name)
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and 0 < expires < now:
            bucket["expired"] += 1
    summary["matches"] = sum(bucket["count"] for bucket in summary["domains"].values())
    return summary
