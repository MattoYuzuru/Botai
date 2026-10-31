"""Playbooks of a source: create the shared or owned record, add versions, activate one.

The service never executes a spec or code: it stores versions (JSON spec and/or parser code) with
tests, a canary result and a checksum, and marks exactly one version active for the runner to pick.
Activation is a pointer move inside a transaction: the previous active version stays for rollback.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.db.models import Job, Playbook, PlaybookVersion, University
from botai.sources.v4 import kinds as kinds_vocab
from botai.sources.v4 import store

TITLE_MAX = 200
CODE_MAX = 200_000  # a parser, not a vendored library; the runner enforces its own sandbox limits


def _validation(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, detail)


def _title(raw: str) -> str:
    value = " ".join(raw.split())
    if not value:
        raise _validation("Название playbook не может быть пустым")
    if len(value) > TITLE_MAX:
        raise _validation("Слишком длинное название playbook")
    return value


def _json_object(raw: object, what: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise _validation(f"{what} должен быть объектом")
    value = dict(raw)
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        raise _validation(f"{what} должен быть JSON-объектом") from None
    return value


def _code(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        raise _validation("Код playbook пуст")
    if len(value) > CODE_MAX:
        raise _validation("Слишком длинный код playbook")
    return value


def _tests(raw: Sequence[object]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise _validation("Тест playbook должен быть объектом")
        value = dict(item)
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            raise _validation("Тест playbook должен быть JSON-объектом") from None
        result.append(value)
    return result


def _checksum(
    spec: Mapping[str, Any] | None, code: str | None, tests: Sequence[Mapping[str, Any]]
) -> str:
    payload = json.dumps(
        {"spec": spec, "code": code, "tests": list(tests)},
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


async def create_playbook(
    session: AsyncSession,
    *,
    user_id: str,
    title: str,
    university_id: str | None = None,
    language: str = "declarative",
    shared: bool = False,
    provenance: Mapping[str, Any] | None = None,
) -> Playbook:
    """Create a playbook. `shared=True` makes it visible to both accounts (`owner_id` NULL, D-018)."""
    if university_id is not None:
        university = await session.scalar(select(University).where(University.id == university_id))
        if university is None:
            raise store.not_found("Вуз не найден")
    playbook = Playbook(
        owner_id=None if shared else user_id,
        university_id=university_id,
        title=_title(title),
        language=kinds_vocab.validate_language(language),
        provenance=_json_object(provenance, "provenance playbook") or {},
    )
    session.add(playbook)
    await session.flush()
    return playbook


async def get_playbook(session: AsyncSession, *, user_id: str, playbook_id: str) -> Playbook:
    """A playbook this user may read: their own or a shared one."""
    return await store.visible_playbook(session, user_id, playbook_id)


async def list_playbooks(
    session: AsyncSession, *, user_id: str, university_id: str | None = None
) -> list[Playbook]:
    """Own and shared playbooks, oldest first; optionally of one university."""
    statement = select(Playbook).where(
        (Playbook.owner_id.is_(None)) | (Playbook.owner_id == user_id)
    )
    if university_id is not None:
        statement = statement.where(Playbook.university_id == university_id)
    statement = statement.order_by(Playbook.created_at, Playbook.id)
    return list(await session.scalars(statement))


async def add_playbook_version(
    session: AsyncSession,
    *,
    user_id: str,
    playbook_id: str,
    spec: Mapping[str, Any] | None = None,
    code: str | None = None,
    tests: Sequence[Mapping[str, Any]] = (),
    canary: Mapping[str, Any] | None = None,
    created_by_run: str | None = None,
) -> PlaybookVersion:
    """Append the next version (inactive). At least one of `spec` and `code` is required.

    The playbook row is locked while the number is computed, so two runners cannot write the same
    version. The checksum covers spec, code and tests: identical content is recognisable even when
    it arrives as a new version.
    """
    playbook = await store.visible_playbook(session, user_id, playbook_id, for_update=True)
    clean_spec = _json_object(spec, "spec playbook")
    clean_code = _code(code)
    if clean_spec is None and clean_code is None:
        raise _validation("У playbook должна быть декларативная часть или код")
    clean_tests = _tests(tests)
    clean_canary = _json_object(canary, "canary playbook")
    if created_by_run is not None:
        job = await session.scalar(
            select(Job).where(Job.id == created_by_run, Job.user_id == user_id)
        )
        if job is None:
            raise store.not_found("Задание не найдено")
    last_version = await session.scalar(
        select(func.max(PlaybookVersion.version)).where(PlaybookVersion.playbook_id == playbook.id)
    )
    version = (last_version or 0) + 1
    row = PlaybookVersion(
        playbook_id=playbook.id,
        version=version,
        spec=clean_spec,
        code=clean_code,
        tests=clean_tests,
        canary=clean_canary,
        created_by_run=created_by_run,
        checksum=_checksum(clean_spec, clean_code, clean_tests),
    )
    session.add(row)
    await session.flush()
    return row


async def activate_playbook_version(
    session: AsyncSession,
    *,
    user_id: str,
    playbook_id: str,
    version: int,
    require_canary: bool = True,
) -> PlaybookVersion:
    """Make one version active: the playbook pointer moves, the previous active version is kept.

    A version becomes active only after a successful canary (`canary.ok is True`, written by
    `record_canary` / the runner). `require_canary=False` is the explicit owner action — a rollback
    to an older version that was active before canaries existed, or a manual override.
    """
    playbook = await store.visible_playbook(session, user_id, playbook_id, for_update=True)
    row = await session.scalar(
        select(PlaybookVersion).where(
            PlaybookVersion.playbook_id == playbook.id,
            PlaybookVersion.version == version,
        )
    )
    if row is None:
        raise store.not_found("Версия playbook не найдена")
    if require_canary and not (isinstance(row.canary, Mapping) and row.canary.get("ok") is True):
        raise BotaiError(
            ErrorCode.CONFLICT,
            "Версия не прошла canary: прогоните canary или активируйте её явно "
            "(require_canary=False)",
        )
    await session.execute(
        update(PlaybookVersion)
        .where(
            PlaybookVersion.playbook_id == playbook.id,
            PlaybookVersion.version != version,
            PlaybookVersion.active.is_(True),
        )
        .values(active=False)
    )
    row.active = True
    playbook.current_version = version
    await session.flush()
    return row


async def active_playbook_version(
    session: AsyncSession, *, user_id: str, playbook_id: str
) -> PlaybookVersion | None:
    """The version the runner takes, if any."""
    playbook = await store.visible_playbook(session, user_id, playbook_id)
    return await session.scalar(
        select(PlaybookVersion).where(
            PlaybookVersion.playbook_id == playbook.id,
            PlaybookVersion.active.is_(True),
        )
    )


async def list_playbook_versions(
    session: AsyncSession, *, user_id: str, playbook_id: str
) -> list[PlaybookVersion]:
    """All versions, newest first (the active one may be older after a rollback)."""
    playbook = await store.visible_playbook(session, user_id, playbook_id)
    statement = (
        select(PlaybookVersion)
        .where(PlaybookVersion.playbook_id == playbook.id)
        .order_by(PlaybookVersion.version.desc())
    )
    return list(await session.scalars(statement))


async def get_playbook_version(
    session: AsyncSession, *, user_id: str, playbook_id: str, version: int
) -> PlaybookVersion:
    """One version of a visible playbook, or `NOT_FOUND`."""
    playbook = await store.visible_playbook(session, user_id, playbook_id)
    row = await session.scalar(
        select(PlaybookVersion).where(
            PlaybookVersion.playbook_id == playbook.id,
            PlaybookVersion.version == version,
        )
    )
    if row is None:
        raise store.not_found("Версия playbook не найдена")
    return row


async def record_canary(
    session: AsyncSession,
    *,
    user_id: str,
    playbook_id: str,
    version: int,
    canary: Mapping[str, Any],
) -> PlaybookVersion:
    """Store the result of a canary run of one version (read by the UI and the next runner)."""
    row = await get_playbook_version(
        session, user_id=user_id, playbook_id=playbook_id, version=version
    )
    row.canary = dict(canary)
    await session.flush()
    return row


async def set_playbook_health(
    session: AsyncSession, *, user_id: str, playbook_id: str, health: Mapping[str, Any]
) -> Playbook:
    """Replace the health summary of a playbook (counts, last run, drift)."""
    playbook = await store.visible_playbook(session, user_id, playbook_id, for_update=True)
    playbook.health = dict(health)
    await session.flush()
    return playbook
