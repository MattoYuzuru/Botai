"""Operator CLI: `python -m botai.auth.cli bootstrap-owner|create-invite|disable-user|enable-user`.

The password is read with getpass (never from argv or the environment). The invite code is
printed once; only its hash is stored.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable, Sequence
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.auth import service
from botai.core.clock import Clock, SystemClock
from botai.core.errors import BotaiError
from botai.db.models import AuthSession, User

MIN_PASSWORD = 8


class CliError(Exception):
    """A mistake of the operator, printed without a traceback."""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="botai.auth.cli", description="BotAI: управление пулом")
    sub = p.add_subparsers(dest="command", required=True)
    owner = sub.add_parser("bootstrap-owner", help="создать владельца (один раз)")
    owner.add_argument("--email", required=True)
    owner.add_argument("--name", required=True)
    invite = sub.add_parser("create-invite", help="выпустить приглашение (код виден один раз)")
    invite.add_argument("--days", type=int, default=7, help="срок действия, по умолчанию 7")
    for name, help_ in (("disable-user", "отозвать доступ"), ("enable-user", "вернуть доступ")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--email", required=True)
    return p


def _read_new_password(read: Callable[[str], str]) -> str:
    password = read("Пароль BotAI: ")
    if len(password) < MIN_PASSWORD:
        raise CliError(f"Пароль должен быть не короче {MIN_PASSWORD} символов")
    if read("Повторите пароль: ") != password:
        raise CliError("Пароли не совпали")
    return password


async def _owner_id(session: AsyncSession) -> str:
    owner = await session.scalar(
        select(User.id).where(User.is_owner.is_(True)).order_by(User.created_at).limit(1)
    )
    if owner is None:
        raise CliError("Владельца ещё нет: сначала bootstrap-owner")
    return owner


async def _set_disabled(session: AsyncSession, email: str, *, disabled: bool) -> None:
    user = await session.scalar(select(User).where(User.email == service.normalize_email(email)))
    if user is None:
        raise CliError("Пользователь не найден")
    if user.is_owner and disabled:
        raise CliError("Владельца отключить нельзя")
    user.disabled = disabled
    if disabled:  # live sessions end at once, not at their natural expiry
        await session.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    await session.commit()


async def run(
    argv: Sequence[str],
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    read_password: Callable[[str], str] = getpass.getpass,
    clock: Clock | None = None,
    out: Callable[[str], None] = print,
) -> int:
    args = _parser().parse_args(argv)
    clock = clock or SystemClock()
    try:
        async with sessionmaker() as session:
            if args.command == "bootstrap-owner":
                user = await service.bootstrap_owner(
                    session,
                    email=args.email,
                    display_name=args.name,
                    password=_read_new_password(read_password),
                )
                out(f"Владелец создан: {user.email}")
            elif args.command == "create-invite":
                code, expires_at = await service.create_invite(
                    session,
                    created_by=await _owner_id(session),
                    now=clock.now(),
                    ttl=timedelta(days=args.days),
                )
                out(f"Код приглашения (показывается один раз): {code}")
                out(f"Действует до {expires_at:%Y-%m-%d %H:%M} UTC")
            else:
                await _set_disabled(session, args.email, disabled=args.command == "disable-user")
                out("Готово")
    except CliError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except BotaiError as exc:
        print(f"Ошибка: {exc.detail}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    from botai.db.session import get_sessionmaker

    raise SystemExit(asyncio.run(run(sys.argv[1:], sessionmaker=get_sessionmaker())))


if __name__ == "__main__":
    main()
