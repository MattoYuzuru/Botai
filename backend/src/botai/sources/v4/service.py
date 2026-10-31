"""Sources of a university: create, read, update, switch off and on.

Aditive to v3: this service writes only the v4 tables (`sources`, `playbooks`, ...) and never
touches `connections`. Callers pass the request's `AsyncSession`; functions flush but do not
commit, so a caller can compose several operations in one transaction (the API or a runner owns the
commit). Identity, kinds and status vocabulary live in `identity` and `kinds`.

A v4 source is unique inside its space by `identity_key`, so two cabinets, tables or calendars of
one university are two rows; the same address twice is a `CONFLICT`, never a silent replace.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.db.models import Source
from botai.sources.v4 import identity, store
from botai.sources.v4 import kinds as kinds_vocab
from botai.sources.v4.schemas import SourcePatch

TITLE_MAX = 200
AUTH_REF_MAX = 200


def _validation(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.VALIDATION, detail)


def _conflict(detail: str) -> BotaiError:
    return BotaiError(ErrorCode.CONFLICT, detail)


def _title(raw: str) -> str:
    value = " ".join(raw.split())
    if not value:
        raise _validation("Название источника не может быть пустым")
    if len(value) > TITLE_MAX:
        raise _validation("Слишком длинное название источника")
    return value


def _config(raw: object | None) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _validation("config источника должен быть объектом")
    return dict(raw)


def _resolve_identity(
    *,
    source_id: str,
    identity_key: str | None,
    link: str | None,
    file_sha256: str | None,
    mailbox: str | None,
    entry_url: str | None,
) -> tuple[str, str | None]:
    """Return `(identity_key, cleaned_entry_url)`; exactly one source of identity is accepted."""
    explicit = [value for value in (identity_key, link, file_sha256, mailbox) if value is not None]
    if len(explicit) > 1:
        raise _validation(
            "Укажите что-то одно: identity, ссылку календаря, отпечаток файла или ящик"
        )
    if identity_key is not None:
        cleaned = identity.normalize_url(entry_url) if entry_url else None
        return identity.validate_identity(identity_key), cleaned
    if link is not None:
        cleaned = identity.normalize_url(entry_url) if entry_url else None
        return identity.calendar_key(link), cleaned
    if file_sha256 is not None:
        return identity.file_key(file_sha256), None
    if mailbox is not None:
        return identity.mail_key(mailbox), None
    if entry_url is not None:
        cleaned = identity.normalize_url(entry_url)
        return cleaned, cleaned
    return identity.provisional_key(source_id), None


async def create_source(
    session: AsyncSession,
    *,
    user_id: str,
    space_id: str,
    title: str,
    entry_url: str | None = None,
    identity_key: str | None = None,
    link: str | None = None,
    file_sha256: str | None = None,
    mailbox: str | None = None,
    kinds: Sequence[str] = (),
    status: str = "draft",
    playbook_id: str | None = None,
    config: Mapping[str, Any] | None = None,
    auth_ref: str | None = None,
    priority: int = 0,
    trust: str = "normal",
) -> Source:
    """Create a v4 source in a space of this user.

    Callers pass nature and data kinds together (`["lms", "schedule", "deadlines"]`). Without an
    address a provisional `draft:<id>` identity is used and may be replaced by `update_source`.
    """
    space = await store.owned_space(session, user_id, space_id)
    source_id = new_id()
    resolved_identity, cleaned_entry_url = _resolve_identity(
        source_id=source_id,
        identity_key=identity_key,
        link=link,
        file_sha256=file_sha256,
        mailbox=mailbox,
        entry_url=entry_url,
    )
    duplicate = await store.source_by_identity(session, space.id, resolved_identity)
    if duplicate is not None:
        raise _conflict(f"Источник с этим адресом уже подключён: «{duplicate.title}»")
    if playbook_id is not None:
        await store.visible_playbook(session, user_id, playbook_id)
    if auth_ref is not None and len(auth_ref) > AUTH_REF_MAX:
        raise _validation("Слишком длинное имя секрета источника")
    source = Source(
        id=source_id,
        user_id=user_id,
        space_id=space.id,
        university_id=space.university_id,
        title=_title(title),
        entry_url=cleaned_entry_url,
        identity_key=resolved_identity,
        kinds=kinds_vocab.normalize_kinds(kinds),
        status=kinds_vocab.validate_status(status),
        playbook_id=playbook_id,
        config=_config(config),
        auth_ref=auth_ref.strip() if auth_ref else None,
        priority=max(0, priority),
        trust=kinds_vocab.validate_trust(trust),
    )
    # A savepoint around the insert: a concurrent writer that won the partial unique index turns
    # into a clean CONFLICT with the first source's title instead of aborting the caller's whole
    # transaction with a 500.
    try:
        async with session.begin_nested():
            session.add(source)
            await session.flush()
    except IntegrityError:
        existing = await store.source_by_identity(session, space.id, resolved_identity)
        title = existing.title if existing is not None else "источник"
        raise _conflict(f"Источник с этим адресом уже подключён: «{title}»") from None
    return source


async def list_sources(
    session: AsyncSession,
    *,
    user_id: str,
    space_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> list[Source]:
    """Sources of the user, oldest first; optionally of one space and/or one status and kind."""
    statement = select(Source).where(Source.user_id == user_id)
    if space_id is not None:
        space = await store.owned_space(session, user_id, space_id)
        statement = statement.where(Source.space_id == space.id)
    if status is not None:
        statement = statement.where(Source.status == kinds_vocab.validate_status(status))
    if kind is not None:
        if kind not in kinds_vocab.ALL_KINDS:
            raise _validation(f"Неизвестный вид источника: {kind}")
        statement = statement.where(Source.kinds.contains([kind]))
    statement = statement.order_by(Source.created_at, Source.id)
    return list(await session.scalars(statement))


async def get_source(session: AsyncSession, *, user_id: str, source_id: str) -> Source:
    """One source of the user; a foreign or missing id is not found."""
    return await store.owned_source(session, user_id, source_id)


async def update_source(
    session: AsyncSession, *, user_id: str, source_id: str, patch: SourcePatch
) -> Source:
    """Apply the fields the caller actually set; an absent field is untouched, `null` clears it."""
    source = await store.owned_source(session, user_id, source_id, for_update=True)
    data = patch.model_dump(exclude_unset=True)

    if "title" in data:
        title = data["title"]
        if title is None:
            raise _validation("Название источника не может быть пустым")
        source.title = _title(str(title))
    if "entry_url" in data:
        raw_entry = data["entry_url"]
        cleaned = identity.normalize_url(str(raw_entry)) if raw_entry is not None else None
        source.entry_url = cleaned
        if "identity" not in data:
            source.identity_key = (
                cleaned if cleaned is not None else identity.provisional_key(source.id)
            )
    if "identity" in data:
        raw_identity = data["identity"]
        if raw_identity is None:
            raise _validation("identity источника нельзя очистить")
        source.identity_key = identity.validate_identity(str(raw_identity))
    if "kinds" in data:
        raw_kinds = data["kinds"]
        source.kinds = kinds_vocab.normalize_kinds(raw_kinds or ())
    if "status" in data:
        raw_status = data["status"]
        if raw_status is None:
            raise _validation("Состояние источника нельзя очистить")
        _set_status(source, kinds_vocab.validate_status(str(raw_status)))
    if "playbook_id" in data:
        raw_playbook = data["playbook_id"]
        if raw_playbook is not None:
            await store.visible_playbook(session, user_id, str(raw_playbook))
        source.playbook_id = str(raw_playbook) if raw_playbook is not None else None
    if "config" in data:
        source.config = _config(data["config"])
    if "auth_ref" in data:
        raw_auth = data["auth_ref"]
        if raw_auth is not None and len(str(raw_auth)) > AUTH_REF_MAX:
            raise _validation("Слишком длинное имя секрета источника")
        source.auth_ref = str(raw_auth).strip() if raw_auth else None
    if "priority" in data:
        raw_priority = data["priority"]
        if raw_priority is None:
            raise _validation("Приоритет источника нельзя очистить")
        source.priority = max(0, int(raw_priority))
    if "trust" in data:
        raw_trust = data["trust"]
        if raw_trust is None:
            raise _validation("Доверие источника нельзя очистить")
        source.trust = kinds_vocab.validate_trust(str(raw_trust))
    if "last_error" in data:
        raw_error = data["last_error"]
        source.last_error = dict(raw_error) if raw_error is not None else None
    if "last_sync_at" in data:
        raw_sync: datetime | None = data["last_sync_at"]
        source.last_sync_at = raw_sync

    duplicate = await store.source_by_identity(session, source.space_id, source.identity_key)
    if duplicate is not None and duplicate.id != source.id:
        raise _conflict(f"Источник с этим адресом уже подключён: «{duplicate.title}»")
    await session.flush()
    return source


async def set_status(
    session: AsyncSession,
    *,
    user_id: str,
    source_id: str,
    status: str,
    last_error: Mapping[str, Any] | None = None,
) -> Source:
    """Move a source to a state (used by the runner); `disabled` remembers what to restore."""
    source = await store.owned_source(session, user_id, source_id, for_update=True)
    _set_status(source, kinds_vocab.validate_status(status))
    if last_error is not None:
        source.last_error = dict(last_error)
    await session.flush()
    return source


def _set_status(source: Source, status: str) -> None:
    if status == "disabled":
        if source.status != "disabled":
            source.disabled_from = source.status
    elif source.status == "disabled":
        source.disabled_from = None
    source.status = status


async def disable_source(session: AsyncSession, *, user_id: str, source_id: str) -> Source:
    """Switch a source off: it stops syncing and its data is hidden, nothing is deleted."""
    source = await store.owned_source(session, user_id, source_id, for_update=True)
    _set_status(source, "disabled")
    await session.flush()
    return source


async def enable_source(session: AsyncSession, *, user_id: str, source_id: str) -> Source:
    """Switch a source back on, restoring the status it had (a source waiting for a login waits)."""
    source = await store.owned_source(session, user_id, source_id, for_update=True)
    if source.status == "disabled":
        restore = source.disabled_from or "draft"
        source.status = restore if restore in kinds_vocab.SOURCE_STATUSES else "draft"
        source.disabled_from = None
    await session.flush()
    return source


async def mark_synced(
    session: AsyncSession,
    *,
    user_id: str,
    source_id: str,
    synced_at: datetime,
    status: str = "active",
) -> Source:
    """Record a successful read: freshness, cleared error and the new state (used by the runner)."""
    source = await store.owned_source(session, user_id, source_id, for_update=True)
    _set_status(source, kinds_vocab.validate_status(status))
    source.last_sync_at = synced_at
    source.last_error = None
    await session.flush()
    return source


async def mark_error(
    session: AsyncSession,
    *,
    user_id: str,
    source_id: str,
    error: Mapping[str, Any],
    status: str = "error",
) -> Source:
    """Record a failed read honestly; prior data and `last_sync_at` stay as they were."""
    source = await store.owned_source(session, user_id, source_id, for_update=True)
    _set_status(source, kinds_vocab.validate_status(status))
    source.last_error = dict(error)
    await session.flush()
    return source
