"""Server-side persistent browser profiles for the v4 agent: a 0700 directory per user/profile.

A profile is not a second session store: the Vault keeps the encrypted session the app reuses,
while a browser profile keeps what only a real Chromium profile can keep — cookies, localStorage,
IndexedDB, service workers and cache, so a site sees the same device across runs. The copy of a
real Chrome profile from the owner's mac is the benchmark's job (`bench/live`), not this module's:
here a profile is either grown by the agent's own browser or seeded from a storage state.

Layout, strictly `0700` directories and `0600` files::

    <root>/<user>/<profile>/chromium/          # Chromium user-data dir (persistent context)
    <root>/<user>/<profile>/storage_state.json # Playwright storage state (cookies + localStorage)

Only non-secret metadata and session material live here, and session material is working material
for the agent (D-008) — but never for `log`: no cookie, token or URL query is ever logged. A
profile open in two browsers at once corrupts the Chromium user-data dir, so `lock()` gives one
exclusive holder (a lock file in the same directory).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from playwright.async_api import Cookie, StorageState

from botai.core.errors import BotaiError, ErrorCode

log = logging.getLogger(__name__)

# Bounds mirror `browser/session.py`: a storage state is small metadata, not a data store.
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_COOKIES = 500
MAX_STORAGE_ORIGINS = 30
MAX_STORAGE_ENTRIES = 500
MAX_COOKIE_VALUE_CHARS = 16_384
MAX_STORAGE_VALUE_CHARS = 32_768
MAX_NAME_CHARS = 64
MAX_KEY_CHARS = 256
PROFILE_LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.05
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
try:  # POSIX only (the backend runs on Linux, development on macOS)
    import fcntl
except ImportError:  # pragma: no cover - Windows is not a deployment target
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    """What a profile directory holds, without opening the browser."""

    user_id: str
    name: str
    path: str
    has_user_data: bool
    has_storage_state: bool
    storage_state_bytes: int
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "path": self.path,
            "has_user_data": self.has_user_data,
            "has_storage_state": self.has_storage_state,
            "storage_state_bytes": self.storage_state_bytes,
            "updated_at": self.updated_at,
        }


class _ProfileLock:
    """Exclusive advisory lock of one profile directory (`fcntl.flock`)."""

    def __init__(self, path: Path, *, timeout_s: float) -> None:
        self._path = path
        self._timeout_s = timeout_s
        self._fd: int | None = None

    async def __aenter__(self) -> _ProfileLock:
        await asyncio.to_thread(self._acquire)
        return self

    async def __aexit__(self, *_: object) -> None:
        await asyncio.to_thread(self._release)

    def _acquire(self) -> None:
        if fcntl is None:  # pragma: no cover - Windows is not a deployment target
            raise BotaiError(ErrorCode.NOT_IMPLEMENTED, "Блокировка профиля не поддерживается")
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise BotaiError(
                        ErrorCode.CONFLICT,
                        "Профиль уже открыт другим браузером: закройте его и повторите",
                    ) from None
                time.sleep(_LOCK_POLL_S)

    def _release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


class BrowserProfileStore:
    """Persistent profiles on the server, one directory per (user, profile)."""

    def __init__(self, root: Path | str | None = None) -> None:
        explicit = root if root is not None else os.environ.get("BOTAI_BROWSER_PROFILES_DIR", "")
        self._root = Path(explicit) if explicit else _default_root()
        self._root = _secure_dir(self._root)

    @property
    def root(self) -> Path:
        return self._root

    # -- paths -------------------------------------------------------------------------------

    def _profile_path(self, user_id: str, name: str) -> Path:
        _check_segment(user_id, "пользователь")
        _check_segment(name, "профиль")
        return self._root / user_id / name

    def profile_dir(self, user_id: str, name: str = "default") -> Path:
        return _secure_dir(_secure_dir(self._profile_path(user_id, name).parent) / name)

    def user_data_dir(self, user_id: str, name: str = "default") -> Path:
        """The Chromium user-data directory of the profile (created 0700, not cleared)."""
        return _secure_dir(self.profile_dir(user_id, name) / "chromium")

    def storage_state_path(self, user_id: str, name: str = "default") -> Path:
        return self._profile_path(user_id, name) / "storage_state.json"

    def lock(
        self, user_id: str, name: str = "default", *, timeout_s: float = PROFILE_LOCK_TIMEOUT_S
    ) -> _ProfileLock:
        """Hold the profile exclusively while a browser runs on it."""
        directory = self.profile_dir(user_id, name)
        return _ProfileLock(directory / ".lock", timeout_s=timeout_s)

    # -- storage state -----------------------------------------------------------------------

    def load_storage_state(self, user_id: str, name: str = "default") -> StorageState | None:
        """Cookies and localStorage saved the last time, or None when the profile is new."""
        path = self.storage_state_path(user_id, name)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Не удалось прочитать состояние профиля"
            ) from exc
        if len(raw) > MAX_STATE_BYTES:
            raise BotaiError(ErrorCode.VALIDATION, "Состояние профиля слишком большое")
        try:
            state: Any = json.loads(raw)
        except ValueError:
            raise BotaiError(ErrorCode.CONFLICT, "Файл профиля повреждён") from None
        return validate_storage_state(state)

    def save_storage_state(self, user_id: str, name: str, state: Mapping[str, Any]) -> Path:
        """Replace the saved state atomically: 0600 file, 0700 directory, no temp left behind."""
        validated = validate_storage_state(state)
        path = self.storage_state_path(user_id, name)
        _write_json_atomic(path, cast(Any, validated))
        log.info("browser profile storage state saved")
        return path

    def import_cookies(self, user_id: str, name: str, cookies: Sequence[Mapping[str, Any]]) -> int:
        """Merge cookies into the saved state (by name+domain+path) and return how many were added.

        This is how a helper (CDP from the owner's mac, a Chrome cookie export) seeds a server
        profile without copying a whole Chrome profile."""
        if len(cookies) > MAX_COOKIES:
            raise BotaiError(ErrorCode.VALIDATION, "Слишком много cookies для одного профиля")
        normalized = [validate_cookie(cookie) for cookie in cookies]
        if not normalized:
            raise BotaiError(ErrorCode.VALIDATION, "Cookies: пустой список")
        state = self.load_storage_state(user_id, name)
        current = list(state.get("cookies") or []) if state else []
        index = {
            (cookie.get("name"), cookie.get("domain"), cookie.get("path")): position
            for position, cookie in enumerate(current)
        }
        added = 0
        for cookie in normalized:
            key = (cookie.get("name"), cookie.get("domain"), cookie.get("path"))
            if key in index:
                current[index[key]] = cast(Any, cookie)
            else:
                if len(current) >= MAX_COOKIES:
                    break
                index[key] = len(current)
                current.append(cast(Any, cookie))
                added += 1
        origins = list(state.get("origins") or []) if state else []
        self.save_storage_state(
            user_id, name, cast(Mapping[str, Any], {"cookies": current, "origins": origins})
        )
        log.info("browser profile cookies imported: %d new", added)
        return added

    # -- listing and removal -----------------------------------------------------------------

    def list(self, user_id: str) -> list[ProfileInfo]:
        _check_segment(user_id, "пользователь")
        user_dir = self._root / user_id
        if not user_dir.is_dir():
            return []
        result: list[ProfileInfo] = []
        for child in sorted(user_dir.iterdir()):
            if not child.is_dir() or not _SEGMENT.match(child.name):
                continue
            state_path = child / "storage_state.json"
            state_size = 0
            if state_path.is_file():
                with contextlib.suppress(OSError):
                    state_size = state_path.stat().st_size
            updated = 0.0
            for candidate in (state_path, child / "chromium"):
                with contextlib.suppress(OSError):
                    updated = max(updated, candidate.stat().st_mtime)
            result.append(
                ProfileInfo(
                    user_id=user_id,
                    name=child.name,
                    path=str(child),
                    has_user_data=(child / "chromium").is_dir(),
                    has_storage_state=state_path.is_file(),
                    storage_state_bytes=state_size,
                    updated_at=updated,
                )
            )
        return result

    def delete(self, user_id: str, name: str = "default") -> bool:
        """Remove a profile directory with its Chromium data and saved state."""
        directory = self._profile_path(user_id, name)
        if not directory.exists():
            return False
        root = (self._root / user_id).resolve()
        if directory.resolve().parent != root:  # never remove anything outside the store
            raise BotaiError(ErrorCode.VALIDATION, "Профиль вне каталога профилей")
        _remove_tree(directory)
        log.info("browser profile deleted")
        return True

    def clear_storage_state(self, user_id: str, name: str = "default") -> bool:
        """Forget cookies/localStorage; the Chromium user-data dir stays as it is."""
        path = self.storage_state_path(user_id, name)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise BotaiError(
                ErrorCode.SOURCE_UNAVAILABLE, "Не удалось удалить состояние профиля"
            ) from exc
        return True


# --------------------------------------------------------------------------------------------
# Validation and filesystem helpers
# --------------------------------------------------------------------------------------------


def validate_storage_state(state: Any) -> StorageState:
    """Accept only a bounded Playwright storage-state shape; reject everything else."""
    if not isinstance(state, Mapping):
        raise BotaiError(ErrorCode.VALIDATION, "Storage state: ожидается объект")
    cookies_raw = state.get("cookies", [])
    origins_raw = state.get("origins", [])
    if not isinstance(cookies_raw, list) or not isinstance(origins_raw, list):
        raise BotaiError(ErrorCode.VALIDATION, "Storage state: cookies/origins — списки")
    if len(cookies_raw) > MAX_COOKIES:
        raise BotaiError(ErrorCode.VALIDATION, "Storage state: слишком много cookies")
    if len(origins_raw) > MAX_STORAGE_ORIGINS:
        raise BotaiError(ErrorCode.VALIDATION, "Storage state: слишком много origin'ов")
    cookies = [_validated_persisted_cookie(item) for item in cookies_raw]
    origins: list[dict[str, Any]] = []
    total = 0
    for raw in origins_raw:
        if not isinstance(raw, Mapping):
            raise BotaiError(ErrorCode.VALIDATION, "Storage state: origin — объект")
        origin = raw.get("origin")
        entries_raw = raw.get("localStorage", [])
        if not isinstance(origin, str) or not isinstance(entries_raw, list):
            raise BotaiError(ErrorCode.VALIDATION, "Storage state: origin и localStorage")
        checked = _check_origin(origin)
        entries: list[dict[str, str]] = []
        for item in entries_raw[:MAX_STORAGE_ENTRIES]:
            if not isinstance(item, Mapping):
                continue
            name, value = item.get("name"), item.get("value")
            if isinstance(name, str) and name and isinstance(value, str):
                entry = {"name": name[:MAX_KEY_CHARS], "value": value[:MAX_STORAGE_VALUE_CHARS]}
                total += len(entry["name"].encode("utf-8")) + len(entry["value"].encode("utf-8"))
                entries.append(entry)
        origins.append({"origin": checked, "localStorage": entries})
        if total > MAX_STATE_BYTES:
            raise BotaiError(ErrorCode.VALIDATION, "Storage state: слишком большой")
    normalized = cast(StorageState, {"cookies": cookies, "origins": origins})
    # The limit is about the serialised file (the load path reads bytes, not entries): cookies
    # and localStorage each pass their own bounds, but together they must still fit.
    serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(serialized) > MAX_STATE_BYTES:
        raise BotaiError(
            ErrorCode.VALIDATION,
            f"Storage state: слишком большой ({len(serialized)} байт, лимит {MAX_STATE_BYTES})",
        )
    return normalized


def validate_cookie(cookie: Mapping[str, Any]) -> Cookie:
    """A cookie for `context.add_cookies`: name/value/domain or url, bounded."""
    return _validated_persisted_cookie(cookie)


def _validated_persisted_cookie(cookie: Any) -> Cookie:
    if not isinstance(cookie, Mapping):
        raise BotaiError(ErrorCode.VALIDATION, "Cookie: ожидается объект")
    name, value = cookie.get("name"), cookie.get("value")
    if not isinstance(name, str) or not name:
        raise BotaiError(ErrorCode.VALIDATION, "Cookie: нужно имя")
    if not isinstance(value, str):
        raise BotaiError(ErrorCode.VALIDATION, "Cookie: значение — строка")
    domain, url = cookie.get("domain"), cookie.get("url")
    if not (isinstance(domain, str) and domain) and not (isinstance(url, str) and url):
        raise BotaiError(ErrorCode.VALIDATION, "Cookie: нужен domain или url")
    result: dict[str, Any] = {
        "name": name[:MAX_NAME_CHARS],
        "value": value[:MAX_COOKIE_VALUE_CHARS],
    }
    for key in ("domain", "path", "url"):
        raw = cookie.get(key)
        if isinstance(raw, str) and raw:
            result[key] = raw[:2_048]
    expires = cookie.get("expires")
    if isinstance(expires, int | float) and not isinstance(expires, bool):
        result["expires"] = float(expires)
    for key in ("httpOnly", "secure"):
        raw = cookie.get(key)
        if isinstance(raw, bool):
            result[key] = raw
    same_site = cookie.get("sameSite")
    if same_site in ("Lax", "None", "Strict"):
        result["sameSite"] = same_site
    return cast(Cookie, result)


def _check_origin(origin: str) -> str:
    from urllib.parse import urlsplit  # local: only this helper parses URLs

    parts = urlsplit(origin)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise BotaiError(ErrorCode.VALIDATION, "Storage state: недопустимый origin")
    return f"{parts.scheme}://{parts.netloc}"


def _check_segment(value: str, what: str) -> None:
    if not _SEGMENT.match(value):
        raise BotaiError(
            ErrorCode.VALIDATION,
            f"Недопустимое имя {what}: латиница, цифры, точка, дефис и подчёркивание, до 63 символов",
        )


def _default_root() -> Path:
    configured = os.environ.get("BOTAI_BROWSER_PROFILES_DIR", "").strip()
    if configured:
        return Path(configured)
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "botai-v4-browser" / "profiles"


def _secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def _write_json_atomic(path: Path, data: Any) -> None:
    _secure_dir(path.parent)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _remove_tree(directory: Path) -> None:
    for child in sorted(directory.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            if child.is_dir() and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()
    with contextlib.suppress(OSError):
        directory.rmdir()


@contextlib.asynccontextmanager
async def locked_profile(
    store: BrowserProfileStore, user_id: str, name: str = "default"
) -> AsyncGenerator[Path]:
    """`async with locked_profile(store, user, name) as directory:` — exclusive profile use."""
    directory = store.profile_dir(user_id, name)
    async with store.lock(user_id, name):
        yield directory


__all__ = [
    "BrowserProfileStore",
    "ProfileInfo",
    "locked_profile",
    "validate_cookie",
    "validate_storage_state",
]
