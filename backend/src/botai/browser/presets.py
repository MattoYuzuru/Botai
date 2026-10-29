"""Login presets: where they are kept, and how a finished login becomes one.

A preset describes an origin, not a person: step selectors and labels, success and expiry signs,
hosts. `build_preset` never receives a typed value, and every string that came from a page passes
`forms.looks_personal` before it is stored. Presets are validated against
`contracts/login-preset.schema.json` before they are saved.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.browser.forms import FormStep, as_preset_step, login_area, looks_personal, safe_path
from botai.browser.proxy import is_ip_literal, normalize_host
from botai.contracts import schemas
from botai.contracts.specs import LoginPreset, LoginStep
from botai.core.browser import SessionState
from botai.core.errors import BotaiError, ErrorCode
from botai.core.execution import GuardedJobContext, guard_authentication_job
from botai.core.ids import new_id
from botai.db.models import Connection, LoginPresetRow

MAX_STEPS = 8
MAX_HOSTS = 12


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    origin: str
    allow_hosts: tuple[str, ...]
    preset_id: str | None = None


class LoginStore(Protocol):
    async def connection(self, user_id: str, connection_id: str) -> ConnectionInfo:
        """The user's connection, or BotaiError(not_found)."""
        ...

    async def allow_host(self, user_id: str, connection_id: str, host: str) -> None:
        """Add the exact DNS host the connection's owner just approved; preserve other hosts."""
        ...

    async def preset_for(self, origin: str) -> LoginPreset | None:
        """The newest active preset for this origin."""
        ...

    async def save_preset(self, origin: str, preset: dict[str, Any], *, connection_id: str) -> int:
        """Store a new revision (older ones become deprecated), link the connection; revision."""
        ...


def normalize_origin(origin: str) -> str:
    parts = urlsplit(origin if "://" in origin else f"https://{origin}")
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and parts.port != 443 else ""
    return f"https://{host}{port}"


def _with_allowed_host(hosts: Sequence[str], host: str) -> tuple[str, ...]:
    clean = normalize_host(host)
    if clean is None or is_ip_literal(clean):
        raise BotaiError(ErrorCode.VALIDATION, "Ожидалось точное DNS-имя сайта")
    merged = tuple(dict.fromkeys([*hosts, clean]))
    if len(merged) > MAX_HOSTS:
        raise BotaiError(ErrorCode.EGRESS_BLOCKED, "Достигнут предел адресов этого подключения")
    return merged


class InMemoryLoginStore:
    """For tests and for flows that run without a database."""

    def __init__(self) -> None:
        self.connections: dict[tuple[str, str], ConnectionInfo] = {}
        self.presets: dict[str, list[dict[str, Any]]] = {}
        self.linked: dict[str, int] = {}

    def add_connection(
        self,
        user_id: str,
        connection_id: str,
        origin: str,
        allow_hosts: Sequence[str] = (),
    ) -> None:
        host = urlsplit(origin).hostname or ""
        self.connections[(user_id, connection_id)] = ConnectionInfo(
            normalize_origin(origin), tuple(allow_hosts) or (host,)
        )

    def seed_preset(self, origin: str, preset: dict[str, Any]) -> None:
        self.presets.setdefault(normalize_origin(origin), []).append(preset)

    async def connection(self, user_id: str, connection_id: str) -> ConnectionInfo:
        info = self.connections.get((user_id, connection_id))
        if info is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
        return info

    async def preset_for(self, origin: str) -> LoginPreset | None:
        rows = self.presets.get(normalize_origin(origin))
        return LoginPreset.model_validate(rows[-1]) if rows else None

    async def allow_host(self, user_id: str, connection_id: str, host: str) -> None:
        info = await self.connection(user_id, connection_id)
        self.connections[(user_id, connection_id)] = replace(
            info, allow_hosts=_with_allowed_host(info.allow_hosts, host)
        )

    async def save_preset(self, origin: str, preset: dict[str, Any], *, connection_id: str) -> int:
        rows = self.presets.setdefault(normalize_origin(origin), [])
        revision = len(rows) + 1
        rows.append({**preset, "revision": revision})
        self.linked[connection_id] = revision
        return revision


class PgLoginStore:
    def __init__(self, db: async_sessionmaker[AsyncSession]) -> None:
        self._db = db

    async def connection(self, user_id: str, connection_id: str) -> ConnectionInfo:
        async with self._db() as session:
            row = (
                await session.execute(
                    select(Connection).where(
                        Connection.id == connection_id, Connection.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
        if row.status == "disabled":
            raise BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено")
        return ConnectionInfo(
            normalize_origin(row.origin), tuple(row.allow_hosts or ()), row.login_preset_id
        )

    async def allow_host(
        self, user_id: str, connection_id: str, host: str, *, ctx: object | None = None
    ) -> None:
        if isinstance(ctx, GuardedJobContext) and (
            ctx.user_id != user_id or ctx.connection_id != connection_id
        ):
            raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
        async with self._db() as session:
            row = (
                await session.execute(
                    select(Connection)
                    .where(Connection.id == connection_id, Connection.user_id == user_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
            if row.status == "disabled":
                raise BotaiError(ErrorCode.FORBIDDEN, "Подключение отключено")
            await guard_authentication_job(ctx, session)
            row.allow_hosts = list(_with_allowed_host(row.allow_hosts or (), host))
            await session.commit()

    async def preset_for(self, origin: str) -> LoginPreset | None:
        async with self._db() as session:
            row = (
                await session.execute(
                    select(LoginPresetRow)
                    .where(
                        LoginPresetRow.origin == normalize_origin(origin),
                        LoginPresetRow.state == "active",
                    )
                    .order_by(LoginPresetRow.revision.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return LoginPreset.model_validate(row.preset) if row else None

    async def save_preset(
        self,
        origin: str,
        preset: dict[str, Any],
        *,
        connection_id: str,
        ctx: object | None = None,
    ) -> int:
        key = normalize_origin(origin)
        for attempt in (1, 2):  # a concurrent login of another student may take our revision
            async with self._db() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"login-preset:{key}"},
                )
                connection = await session.scalar(
                    select(Connection).where(Connection.id == connection_id).with_for_update()
                )
                if connection is None or connection.status == "disabled":
                    raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
                if isinstance(ctx, GuardedJobContext) and (
                    connection.user_id != ctx.user_id or ctx.connection_id != connection_id
                ):
                    raise BotaiError(ErrorCode.NOT_FOUND, "Подключение не найдено")
                await guard_authentication_job(ctx, session)
                latest = (
                    await session.execute(
                        select(LoginPresetRow.revision)
                        .where(LoginPresetRow.origin == key)
                        .order_by(LoginPresetRow.revision.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                revision = (latest or 0) + 1
                row_id = new_id()
                try:
                    await session.execute(
                        update(LoginPresetRow)
                        .where(LoginPresetRow.origin == key, LoginPresetRow.state == "active")
                        .values(state="deprecated")
                    )
                    session.add(
                        LoginPresetRow(
                            id=row_id,
                            origin=key,
                            revision=revision,
                            state="active",
                            preset={**preset, "revision": revision},
                        )
                    )
                    await session.flush()
                    await session.execute(
                        update(Connection)
                        .where(Connection.id == connection_id)
                        .values(login_preset_id=row_id)
                    )
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    if attempt == 2:
                        raise
                    continue
                return revision
        raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------------------------
# Recording a login and turning it into a preset
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Recorder:
    """What happened during one login, without any typed value."""

    forms: list[FormStep] = field(default_factory=lambda: list[FormStep]())
    login_paths: list[str] = field(default_factory=lambda: list[str]())
    mfa_kinds: set[str] = field(default_factory=lambda: set[str]())
    captcha_seen: bool = False
    mirrored: int = 0
    by_human: int = 0
    labels: list[str] = field(default_factory=lambda: list[str]())

    def form(self, step: FormStep) -> None:
        self.mirrored += 1
        self.login_paths.append(urlsplit(step.url).path)
        if all(s.id != step.id for s in self.forms):
            self.forms.append(step)
        for f in step.fields:
            if f.role == "otp":
                self.mfa_kinds.add(_otp_kind(f.label))

    def steps(self, avoid: Sequence[str] = ()) -> list[LoginStep]:
        """The learned form steps, made safe to store."""
        learned = (as_preset_step(form, avoid) for form in self.forms)
        return [step for step in learned if step is not None]

    def push(self, url: str) -> None:
        self.mirrored += 1
        self.mfa_kinds.add("push")
        self.login_paths.append(urlsplit(url).path)

    def captcha(self, url: str) -> None:
        self.captcha_seen = True
        self.login_paths.append(urlsplit(url).path)

    def human(self) -> None:
        self.by_human += 1


def _otp_kind(label: str) -> str:
    low = label.lower()
    if "sms" in low or "смс" in low:
        return "sms"
    if "почт" in low or "mail" in low:
        return "email"
    if "приложен" in low or "authenticator" in low or "totp" in low:
        return "totp"
    return "other"


def preset_id_for(host: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", host.lower()).strip("-")[:48]
    return slug if len(slug) >= 2 else f"{slug}x"


def build_preset(
    *,
    origin: str,
    start_url: str,
    hosts: Sequence[str],
    recorder: Recorder,
    session: SessionState,
    probe: dict[str, Any] | None,
    final_url: str,
    now: datetime,
    avoid: Sequence[str] = (),
) -> dict[str, Any]:
    """The preset for this origin. Raises ValueError if it would not pass the schema."""
    host = urlsplit(origin).hostname or "site"
    allow = list(dict.fromkeys(hosts))[:MAX_HOSTS]
    areas = list(dict.fromkeys(login_area(p, avoid) for p in recorder.login_paths))[:MAX_STEPS]
    success: dict[str, Any] = {}
    if probe is not None:
        success["probe"] = probe
    final = urlsplit(final_url)
    if final.hostname:
        success["urlOrigin"] = f"https://{final.hostname.lower()}"
    if areas:
        success["urlPathNot"] = areas
    if not success:
        success["urlOrigin"] = normalize_origin(origin)
    expired: dict[str, Any] = {"redirectPathPrefix": areas}
    if probe is not None:
        expired["probe401"] = [urlsplit(probe["url"]).path]
    cookie_hosts = [
        h
        for h in allow
        if any(
            (c.domain.startswith(".") and (h == c.domain[1:] or h.endswith(c.domain)))
            or h == c.domain.lstrip(".")
            for c in session.cookies
        )
    ] or allow[:1]
    capture: dict[str, Any] = {
        "cookiesFor": cookie_hosts,
        "bearerFrom": "authorizationHeader" if session.bearer else "none",
        "storage": "encrypted",
        "lifetimeKnown": session.expires_hint is not None,
    }
    if session.expires_hint is not None:
        capture["lifetimeHintDays"] = max(
            0.0, round((session.expires_hint - now).total_seconds() / 86400, 2)
        )
    mfa: dict[str, Any] = {
        "possible": "yes" if recorder.mfa_kinds else "unknown",
        "automate": False,
    }
    if recorder.mfa_kinds:
        mfa["kinds"] = sorted(recorder.mfa_kinds)
    preset: dict[str, Any] = {
        "id": preset_id_for(host),
        "name": host[:200],
        "start": {"url": start_url},
        "allowHosts": allow,
        "idp": {"kind": "form" if len(allow) == 1 else "unknown"},
        "success": success,
        "expired": expired,
        "mfa": mfa,
        "captcha": {"possible": "yes" if recorder.captcha_seen else "unknown", "automate": False},
        "capture": capture,
        "mode": "screen" if recorder.by_human and not recorder.mirrored else "assisted",
        "steps": [
            s.model_dump(by_alias=True, exclude_none=True)
            for s in recorder.steps(avoid)[:MAX_STEPS]
        ],
    }
    problems = schemas.errors("login-preset.schema.json", preset)
    if problems:
        raise ValueError("learned preset does not match the schema: " + "; ".join(problems))
    return preset


def keep_known(preset: dict[str, Any], known: LoginPreset) -> dict[str, Any]:
    """The preset of a login that replayed `known`, without forgetting what the login did not meet.

    A login meets only the pages it meets: a site that trusts this device asks for no SMS code,
    a student who went through a captcha once sees none the next time. The preset is shared by
    every student of the origin, so a run must not make it forget the pages, the second factor or
    the captcha an earlier run saw. What the run did meet replaces its old self (same step id).
    """
    old = [s.model_dump(by_alias=True, exclude_none=True) for s in known.steps]
    fresh = {s["id"]: s for s in preset["steps"]}
    preset["steps"] = ([fresh.pop(s["id"], s) for s in old] + list(fresh.values()))[:MAX_STEPS]

    if known.mfa.get("possible") == "yes":
        kinds = sorted({*known.mfa.get("kinds", []), *preset["mfa"].get("kinds", [])})
        preset["mfa"] = {**preset["mfa"], "possible": "yes", **({"kinds": kinds} if kinds else {})}
    if known.captcha.get("possible") == "yes":
        preset["captcha"] = {**preset["captcha"], "possible": "yes"}

    def union(newer: list[str], older: object) -> list[str]:
        both = [*(older if isinstance(older, list) else []), *newer]
        return list(dict.fromkeys(p for p in both if isinstance(p, str)))[:MAX_STEPS]

    # the pages that mean "logged out", from the pages every run has met
    if "urlPathNot" in preset["success"] or "urlPathNot" in known.success:
        preset["success"]["urlPathNot"] = union(
            preset["success"].get("urlPathNot", []), known.success.get("urlPathNot")
        )
    preset["expired"]["redirectPathPrefix"] = union(
        preset["expired"].get("redirectPathPrefix", []), known.expired.get("redirectPathPrefix")
    )
    problems = schemas.errors("login-preset.schema.json", preset)
    if problems:
        raise ValueError("merged preset does not match the schema: " + "; ".join(problems))
    return preset


def comparable(preset: dict[str, Any] | LoginPreset) -> dict[str, Any]:
    """The part of a preset that decides whether a new revision is worth saving: what the site
    asks for, not how long this particular session happens to live."""
    model = preset if isinstance(preset, LoginPreset) else LoginPreset.model_validate(preset)
    data = model.model_dump(by_alias=True, exclude_none=True)
    for key in ("revision", "id"):
        data.pop(key, None)
    data["allowHosts"] = sorted(data["allowHosts"])
    capture = dict(data["capture"])
    capture.pop("lifetimeHintDays", None)
    capture["cookiesFor"] = sorted(capture.get("cookiesFor", []))
    data["capture"] = capture
    if "kinds" in data.get("mfa", {}):
        data["mfa"] = {**data["mfa"], "kinds": sorted(data["mfa"]["kinds"])}
    return data


def safe_probe_path(url: str) -> str | None:
    """Path of a probe URL if it carries no id-like part, else None."""
    path = urlsplit(url).path
    if not path or looks_personal(path) or safe_path(path) != path:
        return None
    return path
