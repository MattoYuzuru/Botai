"""LoginFlow on the real Vault and the real tables (markers `browser` and `db`).

Security check C1 as `security.md` §8 words it: after a fixture login with the marker password and
code, a dump of the whole database holds neither marker; `secrets` holds ciphertext only.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from botai.browser.egress import ProxyEgress
from botai.browser.gateway import PlaywrightGateway
from botai.browser.login import LoginFlow
from botai.browser.presets import PgLoginStore
from botai.core.browser import SessionState
from botai.core.ids import new_id
from botai.core.jobs import InMemoryJobContext
from botai.core.vault import SecretKind, generate_kek_b64, load_kek
from botai.db.base import Base
from botai.db.models import Connection, Space, User
from botai.vault.pg import PgVault
from tests.auth.support import tx_sessionmaker  # noqa: F401 - fixture
from tests.browser.test_login import FAST, ORIGIN, Student, works
from tests.fakeuni import data, login

pytestmark = [pytest.mark.browser, pytest.mark.db]

Sm = async_sessionmaker[AsyncSession]


async def make_connection(sm: Sm) -> tuple[str, str]:
    async with sm() as session:
        user = User(id=new_id(), email=f"{new_id()}@t.test", display_name="t", password_hash="x")
        session.add(user)
        await session.flush()
        space = Space(id=new_id(), user_id=user.id, title="Вуз")
        session.add(space)
        await session.flush()
        conn = Connection(
            id=new_id(),
            user_id=user.id,
            space_id=space.id,
            origin=ORIGIN,
            kind="portal",
            title="Кабинет",
            allow_hosts=["fakeuni.test"],
        )
        session.add(conn)
        await session.commit()
        return user.id, conn.id


async def dump(sm: Sm) -> str:
    """Every row of every table, as text: what a database dump would hold."""
    parts: list[str] = []
    async with sm() as session:
        for table in Base.metadata.sorted_tables:
            rows = (await session.execute(text(f'select * from "{table.name}"'))).all()  # noqa: S608
            for row in rows:
                parts.append(f"{table.name}: " + repr(tuple(row)))
    return "\n".join(parts)


async def test_no_marker_reaches_the_database_and_secrets_hold_only_ciphertext(
    tx_sessionmaker: Sm,  # noqa: F811
    gateway: PlaywrightGateway,
    egress_access: ProxyEgress,
    trust_fakeuni: Any,
) -> None:
    kek = load_kek(generate_kek_b64())
    vault = PgVault(tx_sessionmaker, {"k1": kek}, "k1")
    user, conn = await make_connection(tx_sessionmaker)
    flow = LoginFlow(
        gateway=gateway,
        vault=vault,
        store=PgLoginStore(tx_sessionmaker),
        egress=egress_access,
        timing=FAST,
    )

    ctx = InMemoryJobContext(user_id=user, connection_id=conn, responder=Student())
    session = await flow.ensure(ctx, user_id=user, connection_id=conn)
    assert await works(session)

    # the Vault holds what the flow stored, readable only through the key
    for kind in (SecretKind.SESSION, SecretKind.LOGIN, SecretKind.PASSWORD):
        assert await vault.has(user, conn, kind), kind
    assert (await vault.get(user, conn, SecretKind.PASSWORD)).get_secret_value() == data.PASSWORD  # type: ignore[union-attr]

    snapshot = await dump(tx_sessionmaker)
    assert "secrets:" in snapshot
    for marker in (data.PASSWORD, data.OTP):
        assert marker not in snapshot, f"{marker} reached the database in the clear"
    assert session.cookies[0].value.get_secret_value() not in snapshot
    async with tx_sessionmaker() as db:
        stored = (
            await db.execute(text("select kind, ciphertext, nonce, wrapped_dek from secrets"))
        ).all()
    assert {row.kind for row in stored} >= {"session", "login", "password"}
    for row in stored:
        for blob in (row.ciphertext, row.nonce, row.wrapped_dek):
            assert isinstance(blob, bytes)
            assert data.PASSWORD.encode() not in blob
            assert data.OTP.encode() not in blob

    # the learned preset is in the table, linked to the connection, and says nothing personal
    async with tx_sessionmaker() as db:
        presets = (
            await db.execute(text("select origin, revision, state, preset from login_presets"))
        ).all()
        linked = (
            await db.execute(
                text("select login_preset_id from connections where id = :c"), {"c": conn}
            )
        ).scalar_one()
    assert [(p.origin, p.revision, p.state) for p in presets] == [(ORIGIN, 1, "active")]
    assert linked is not None
    body = json.dumps(presets[0].preset, ensure_ascii=False)
    for personal in (data.USERNAME, "student", "@", "Иванова"):
        assert personal not in body


async def test_second_login_reuses_the_stored_password_and_the_stored_preset(
    tx_sessionmaker: Sm,  # noqa: F811
    gateway: PlaywrightGateway,
    egress_access: ProxyEgress,
    trust_fakeuni: Any,
) -> None:
    vault = PgVault(tx_sessionmaker, {"k1": load_kek(generate_kek_b64())}, "k1")
    user, conn = await make_connection(tx_sessionmaker)
    flow = LoginFlow(
        gateway=gateway,
        vault=vault,
        store=PgLoginStore(tx_sessionmaker),
        egress=egress_access,
        timing=FAST,
    )
    first = await flow.ensure(
        InMemoryJobContext(user_id=user, connection_id=conn, responder=Student()),
        user_id=user,
        connection_id=conn,
    )
    login.expire_all_sessions()
    student = Student(password="never used", username="never used")
    second: SessionState = await flow.ensure(
        InMemoryJobContext(user_id=user, connection_id=conn, responder=student),
        user_id=user,
        connection_id=conn,
    )
    assert await works(second)
    assert second.cookies[0].value != first.cookies[0].value
    assert student.asked == [("code", "one-time-code")]  # login and password came from the Vault
    async with tx_sessionmaker() as db:
        revisions = (await db.execute(text("select count(*) from login_presets"))).scalar_one()
    assert revisions == 1  # the preset was replayed and did not change
