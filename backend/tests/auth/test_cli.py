"""Operator CLI: bootstrap-owner, create-invite, disable-user."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select, text

from botai.auth import cli
from botai.core.clock import FrozenClock
from botai.db.models import AuthSession, User
from tests.auth.support import PASSWORD, Sm, signup

Make = Callable[[], httpx.AsyncClient]


class Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _password(*answers: str) -> Callable[[str], str]:
    queue = list(answers)
    return lambda _prompt: queue.pop(0)


async def _bootstrap(sm: Sm, out: Console, email: str = "owner@example.com") -> int:
    return await cli.run(
        ["bootstrap-owner", "--email", email, "--name", "Владелец"],
        sessionmaker=sm,
        read_password=_password(PASSWORD, PASSWORD),
        out=out,
    )


async def test_bootstrap_owner_creates_a_hashed_owner(tx_sessionmaker: Sm) -> None:
    out = Console()
    assert await _bootstrap(tx_sessionmaker, out) == 0
    async with tx_sessionmaker() as s:
        owner = (await s.execute(select(User))).scalar_one()
        dump = (await s.execute(text("select users::text from users"))).scalars().all()
    assert owner.is_owner
    assert owner.email == "owner@example.com"
    assert owner.password_hash.startswith("$argon2id$")
    assert PASSWORD not in out.text
    assert all(PASSWORD not in row for row in dump)


async def test_bootstrap_owner_works_only_once(
    tx_sessionmaker: Sm, capsys: pytest.CaptureFixture[str]
) -> None:
    out = Console()
    assert await _bootstrap(tx_sessionmaker, out) == 0
    assert await _bootstrap(tx_sessionmaker, out, "second@example.com") == 1
    assert "уже создан" in capsys.readouterr().err
    async with tx_sessionmaker() as s:
        assert len((await s.execute(select(User))).all()) == 1


@pytest.mark.parametrize(
    ("answers", "message"),
    [(("short", "short"), "не короче"), ((PASSWORD, "different-pw-123"), "не совпали")],
)
async def test_bootstrap_owner_checks_the_password(
    tx_sessionmaker: Sm,
    capsys: pytest.CaptureFixture[str],
    answers: tuple[str, str],
    message: str,
) -> None:
    code = await cli.run(
        ["bootstrap-owner", "--email", "o@example.com", "--name", "O"],
        sessionmaker=tx_sessionmaker,
        read_password=_password(*answers),
        out=Console(),
    )
    assert code == 1
    err = capsys.readouterr().err
    assert message in err
    assert PASSWORD not in err
    async with tx_sessionmaker() as s:
        assert (await s.execute(select(User))).first() is None


async def test_create_invite_prints_a_code_that_registers_a_friend(
    tx_sessionmaker: Sm, clock: FrozenClock, client: httpx.AsyncClient
) -> None:
    out = Console()
    await _bootstrap(tx_sessionmaker, out)
    out.lines.clear()
    assert await cli.run(["create-invite"], sessionmaker=tx_sessionmaker, clock=clock, out=out) == 0
    code = out.lines[0].rsplit(" ", 1)[-1]
    assert out.lines[1].endswith("UTC")
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "inviteCode": code,
            "email": "friend@example.com",
            "displayName": "Друг",
            "password": PASSWORD,
        },
    )
    assert r.status_code == 201


async def test_create_invite_needs_an_owner(
    tx_sessionmaker: Sm, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli.run(["create-invite"], sessionmaker=tx_sessionmaker, out=Console()) == 1
    assert "bootstrap-owner" in capsys.readouterr().err


async def test_disable_user_revokes_access_and_enable_restores_it(
    tx_sessionmaker: Sm, clock: FrozenClock, new_client: Make
) -> None:
    friend = new_client()
    acc = await signup(friend, tx_sessionmaker, clock)
    out = Console()
    code = await cli.run(
        ["disable-user", "--email", acc.email.upper()], sessionmaker=tx_sessionmaker, out=out
    )
    assert code == 0
    assert (await friend.get("/api/v1/me")).status_code == 401
    async with tx_sessionmaker() as s:
        assert (await s.execute(select(AuthSession))).first() is None  # sessions are gone

    login = {"email": acc.email, "password": PASSWORD}
    assert (await new_client().post("/api/v1/auth/login", json=login)).status_code == 401
    await cli.run(["enable-user", "--email", acc.email], sessionmaker=tx_sessionmaker, out=out)
    assert (await new_client().post("/api/v1/auth/login", json=login)).status_code == 200


async def test_owner_cannot_be_disabled(
    tx_sessionmaker: Sm, capsys: pytest.CaptureFixture[str]
) -> None:
    out = Console()
    await _bootstrap(tx_sessionmaker, out)
    code = await cli.run(
        ["disable-user", "--email", "owner@example.com"], sessionmaker=tx_sessionmaker, out=out
    )
    assert code == 1
    assert "Владельца" in capsys.readouterr().err
    async with tx_sessionmaker() as s:
        assert (await s.execute(select(User.disabled))).scalar_one() is False


async def test_disable_unknown_user(
    tx_sessionmaker: Sm, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await cli.run(
        ["disable-user", "--email", "ghost@example.com"],
        sessionmaker=tx_sessionmaker,
        out=Console(),
    )
    assert code == 1
    assert "не найден" in capsys.readouterr().err
