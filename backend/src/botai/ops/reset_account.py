"""Operator command: clear the product data of one account and keep the account.

    python -m botai.ops.reset_account --email E [--shared] [--yes]

Deletes, in one transaction, everything the user produced: threads and messages, jobs with
their events, answers and checkpoints, sources and every domain row read from them, spaces,
documents and their text, facts, attachments, the agent's memory and the user's secrets in the
Vault. `users`, `auth_sessions` and `invites` stay (the user is still signed in), and so do the
audit log and the usage ledger. A `product_data_reset` event is written to the audit log: the
PWA reads it as a marker to clear its own queue and snapshots (`contracts/account-reset.md`).

`--shared` also removes what this account brought into the shared tables and nobody else uses:
the recipes, login presets and site maps of the origins of its sources (and of its documents),
and the universities of its spaces. Whatever the account never touched stays.

The tables are found in the schema, not listed here: every table with a `user_id` (or, like the
events of a job, a foreign key to one that has) is user data and goes. A table that is none of
those, nor deliberately kept, stops the command: a new table must be classified before an
account can be cleared. Without `--yes` nothing is deleted; the counts are printed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, MetaData, Table, delete, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.auth import service
from botai.core.clock import Clock, SystemClock
from botai.db.base import Base
from botai.db.models import AuditLog, Job, User

# The account itself and the records that must outlive a reset.
PRESERVED = frozenset({"users", "auth_sessions", "invites", "audit_log", "llm_usage"})
# Shared between users: removed by `--shared` only when nobody else uses them.
ORIGIN_TABLES = ("adapter_specs", "login_presets", "source_sitemaps")
SHARED = frozenset({*ORIGIN_TABLES, "universities"})
ACTIVE_JOB_STATES = ("queued", "running", "waiting_user")


class ResetError(Exception):
    """A mistake of the operator or an unclassified table, printed without a traceback."""


def user_tables(
    metadata: MetaData, user_id: str
) -> tuple[dict[str, ColumnElement[bool]], list[str]]:
    """Which rows of each table belong to the user, and the tables nobody has classified."""
    owned: dict[str, ColumnElement[bool]] = {}
    unclassified: list[str] = []
    for table in metadata.sorted_tables:  # parents first, so an owner is known before its rows
        if table.name in PRESERVED or table.name in SHARED:
            continue
        if "user_id" in table.c:
            owned[table.name] = table.c.user_id == user_id
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name in owned:
                owner = fk.column.table.name
                owned[table.name] = fk.parent.in_(select(fk.column).where(owned[owner]))
                break
        else:
            unclassified.append(table.name)
    return owned, unclassified


async def _count_or_delete(
    session: AsyncSession, table: Table, where: ColumnElement[bool], *, apply: bool
) -> int:
    count = int(await session.scalar(select(func.count()).select_from(table).where(where)) or 0)
    if apply and count:
        await session.execute(delete(table).where(where))
    return count


@dataclass(frozen=True, slots=True)
class Candidates:
    """What only this user can have brought into the shared tables, read before anything of the
    user is deleted: the sites and documents of their sources, and their universities."""

    origins: list[str]
    spec_ids: list[str]
    preset_ids: list[str]
    university_ids: list[str]


async def _candidates(session: AsyncSession, metadata: MetaData, user_id: str) -> Candidates:
    conn, spaces = metadata.tables["connections"], metadata.tables["spaces"]
    rows = (
        await session.execute(
            select(conn.c.id, conn.c.origin, conn.c.spec_id, conn.c.login_preset_id).where(
                conn.c.user_id == user_id
            )
        )
    ).all()
    universities = await session.scalars(
        select(spaces.c.university_id)
        .where(spaces.c.user_id == user_id, spaces.c.university_id.is_not(None))
        .distinct()
    )
    return Candidates(
        origins=sorted({r.origin for r in rows if r.origin} | {f"doc:{r.id}" for r in rows}),
        spec_ids=sorted({r.spec_id for r in rows if r.spec_id}),
        preset_ids=sorted({r.login_preset_id for r in rows if r.login_preset_id}),
        university_ids=sorted(set(universities)),
    )


async def reset_account(
    session: AsyncSession, user_id: str, *, shared: bool, apply: bool
) -> tuple[dict[str, int], dict[str, int]]:
    """Counts of what was (or, without `apply`, would be) deleted: the user's rows and the
    shared rows nobody else uses. The caller commits."""
    metadata = Base.metadata
    owned, unclassified = user_tables(metadata, user_id)
    if unclassified:
        raise ResetError(
            "Таблицы без владельца и без решения о сохранении: "
            + ", ".join(sorted(unclassified))
            + ". Отнесите их в botai.ops.reset_account."
        )
    candidates = await _candidates(session, metadata, user_id) if shared else None
    deleted: dict[str, int] = {}
    for table in reversed(metadata.sorted_tables):  # children first, so the counts are exact
        if table.name in owned:
            deleted[table.name] = await _count_or_delete(
                session, table, owned[table.name], apply=apply
            )
    removed: dict[str, int] = {}
    if candidates is not None:
        removed = await _delete_unused_shared(session, metadata, user_id, candidates, apply=apply)
    return deleted, removed


async def _delete_unused_shared(
    session: AsyncSession,
    metadata: MetaData,
    user_id: str,
    candidates: Candidates,
    *,
    apply: bool,
) -> dict[str, int]:
    """Recipes, presets and site maps of the user's own sites, and the user's universities, but
    only those nobody else uses. What the user never touched stays, however unused it looks.
    "Used" is judged by everyone else's sources, so a dry run and the real run agree."""
    tables = metadata.tables
    conn = tables["connections"]
    others = conn.c.user_id != user_id
    origins = select(conn.c.origin).where(others)
    documents = select(conn.c.id).where(others)  # a document's recipe lives under `doc:<id>`
    removed: dict[str, int] = {}
    for name in ORIGIN_TABLES:
        table = tables.get(name)
        if table is None:
            continue
        origin = table.c.origin
        theirs = origin.in_(candidates.origins)
        if name == "adapter_specs":
            theirs = theirs | table.c.id.in_(candidates.spec_ids)
        elif name == "login_presets":
            theirs = theirs | table.c.id.in_(candidates.preset_ids)
        where = (
            theirs
            & not_(origin.in_(origins))
            & not_(origin.like("doc:%") & func.substr(origin, 5).in_(documents))
        )
        if name == "adapter_specs":
            pinned = select(conn.c.spec_id).where(others, conn.c.spec_id.is_not(None))
            where = where & not_(table.c.id.in_(pinned))
        elif name == "login_presets":
            pinned = select(conn.c.login_preset_id).where(
                others, conn.c.login_preset_id.is_not(None)
            )
            where = where & not_(table.c.id.in_(pinned))
        removed[name] = await _count_or_delete(session, table, where, apply=apply)
    universities, spaces = tables["universities"], tables["spaces"]
    in_use = select(spaces.c.university_id).where(
        spaces.c.user_id != user_id, spaces.c.university_id.is_not(None)
    )
    removed["universities"] = await _count_or_delete(
        session,
        universities,
        universities.c.id.in_(candidates.university_ids) & not_(universities.c.id.in_(in_use)),
        apply=apply,
    )
    return removed


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="botai.ops.reset_account",
        description="BotAI: очистить продуктовые данные одного аккаунта (аккаунт и вход остаются)",
    )
    p.add_argument("--email", required=True, help="почта аккаунта")
    p.add_argument(
        "--shared",
        action="store_true",
        help="убрать и общее, чем больше никто не пользуется: рецепты, пресеты, карты сайтов, вузы",
    )
    p.add_argument("--yes", action="store_true", help="выполнить; без него печатаются только числа")
    return p


def _lines(title: str, counts: dict[str, int]) -> list[str]:
    rows = [f"  {name}: {count}" for name, count in counts.items() if count]
    return [title, *(rows or ["  ничего"])]


async def run(
    argv: Sequence[str],
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    clock: Clock | None = None,
    out: Callable[[str], None] = print,
) -> int:
    args = _parser().parse_args(argv)
    clock = clock or SystemClock()
    try:
        async with sessionmaker() as session, session.begin():
            user = await session.scalar(
                select(User).where(User.email == service.normalize_email(args.email))
            )
            if user is None:
                raise ResetError("Пользователь не найден")
            active = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.user_id == user.id, Job.state.in_(ACTIVE_JOB_STATES))
                )
                or 0
            )
            deleted, removed = await reset_account(
                session, user.id, shared=args.shared, apply=args.yes
            )
            if args.yes:
                session.add(
                    AuditLog(
                        user_id=user.id,
                        kind="product_data_reset",
                        detail={
                            "deleted": {k: v for k, v in deleted.items() if v},
                            "shared": {k: v for k, v in removed.items() if v},
                        },
                        created_at=clock.now(),
                    )
                )
        verb = "Удалено" if args.yes else "Будет удалено (без --yes ничего не удалено)"
        out(f"Аккаунт: {user.email}")
        if active:
            out(
                f"Внимание: активных заданий {active}. Остановите API и worker BotAI и "
                "повторите, иначе они создадут данные заново."
            )
        for line in _lines(f"{verb}:", deleted):
            out(line)
        if args.shared:
            for line in _lines("Общее, чем больше никто не пользуется:", removed):
                out(line)
        if args.yes:
            out("Готово. Аккаунт и вход сохранены; клиенты очистят свою очередь при следующем /me.")
    except ResetError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    from botai.db.session import get_sessionmaker

    raise SystemExit(asyncio.run(run(sys.argv[1:], sessionmaker=get_sessionmaker())))


if __name__ == "__main__":
    main()
