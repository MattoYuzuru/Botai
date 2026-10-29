"""`python -m botai.ops.reset_account`: one account is cleared, the account itself and everyone
else are not touched (FR-053)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import MetaData, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from botai.core.clock import FrozenClock
from botai.core.ids import new_id
from botai.core.services import Services
from botai.core.vault import SecretKind
from botai.db import models as m
from botai.db.base import Base
from botai.ops import reset_account
from tests.auth.support import Sm, make_user, signup

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SECRET = "P@ssw0rd-FIXTURE"
# What a reset must leave alone, whoever the user is.
KEPT = {"users", "auth_sessions", "invites", "audit_log", "llm_usage"}


class Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


async def _shared_recipe(s: AsyncSession, origin: str) -> None:
    """The recipe and the login preset of a site, made once whoever connects it first."""
    if not await s.scalar(select(m.AdapterSpecRow.id).where(m.AdapterSpecRow.origin == origin)):
        s.add(
            m.AdapterSpecRow(origin=origin, adapter="recipe", revision=1, state="active", spec={})
        )
    if not await s.scalar(select(m.LoginPresetRow.id).where(m.LoginPresetRow.origin == origin)):
        s.add(m.LoginPresetRow(origin=origin, revision=1, state="active", preset={}))


async def _seed(sm: Sm, services: Services, user_id: str, *, tag: str, shared_origin: str) -> None:
    """One row of every kind of product data of the user, plus the shared things it used."""
    uni_own, uni_common = f"uni-{tag}-own", "uni-common"
    own_origin = f"https://{tag}.example.org"
    common = {
        "user_id": user_id,
        "source_url": "https://x.example.org/a",
        "fetched_at": NOW,
        "seen_at": NOW,
    }
    async with sm() as s:
        if await s.get(m.University, uni_common) is None:
            s.add(
                m.University(
                    id=uni_common, title="Общий вуз", short_title="ОВ", origins=[], accent_hue=250
                )
            )
        s.add(
            m.University(
                id=uni_own,
                title=f"Вуз {tag}",
                short_title=tag,
                origins=[own_origin],
                accent_hue=250,
            )
        )
        await s.flush()
        spaces = [
            m.Space(user_id=user_id, university_id=uni_own, title=f"Вуз {tag}"),
            m.Space(user_id=user_id, university_id=uni_common, title="Общий вуз"),
            m.Space(user_id=user_id, university_id=None, title="Мои материалы"),
        ]
        s.add_all(spaces)
        await s.flush()
        own_space, common_space = spaces[0], spaces[1]
        conns = []
        for space, origin in ((own_space, own_origin), (common_space, shared_origin)):
            conn = m.Connection(
                user_id=user_id,
                space_id=space.id,
                origin=origin,
                kind="portal",
                title="Кабинет",
                status="active",
                allow_hosts=[],
            )
            s.add(conn)
            conns.append(conn)
        await s.flush()
        own_conn = conns[0]
        await _shared_recipe(s, own_origin)
        await _shared_recipe(s, shared_origin)
        s.add(
            m.AdapterSpecRow(
                origin=f"doc:{own_conn.id}", adapter="document", revision=1, state="active", spec={}
            )
        )
        s.add_all(
            [
                m.Course(connection_id=own_conn.id, external_id="c", title="Матан", **common),
                m.ScheduleEvent(
                    connection_id=own_conn.id,
                    external_id="e",
                    title="Лекция",
                    starts_at=NOW,
                    ends_at=NOW,
                    **common,
                ),
                m.Deadline(connection_id=own_conn.id, external_id="d", title="ДЗ", **common),
                m.Grade(connection_id=own_conn.id, external_id="g", value="5", **common),
                m.Person(connection_id=own_conn.id, external_id="p", name="Иван", **common),
                m.Location(connection_id=own_conn.id, external_id="l", title="Корпус", **common),
                m.NewsItem(
                    connection_id=own_conn.id,
                    external_id="n",
                    title="Новость",
                    published_at=NOW,
                    **common,
                ),
                m.Assignment(connection_id=own_conn.id, external_id="a", title="Задача", **common),
            ]
        )
        doc = m.Document(
            user_id=user_id,
            space_id=own_space.id,
            connection_id=own_conn.id,
            kind="file",
            title="Правила",
            fetched_at=NOW,
            hash="0" * 64,
        )
        s.add(doc)
        s.add(
            m.Fact(
                user_id=user_id,
                space_id=own_space.id,
                question="q",
                answer="a",
                sources=[],
                confirmed_at=NOW,
                stale_after=NOW,
                confidence="high",
            )
        )
        thread = m.ChatThread(user_id=user_id, space_id=own_space.id, title="Чат")
        s.add(thread)
        await s.flush()
        s.add(m.Chunk(user_id=user_id, document_id=doc.id, ord=0, text="Текст", tokens=1))
        message = m.ChatMessage(user_id=user_id, thread_id=thread.id, role="user", blocks=[])
        s.add(message)
        await s.flush()
        s.add(
            m.Attachment(
                user_id=user_id,
                thread_id=thread.id,
                message_id=message.id,
                name="a.png",
                mime="image/png",
                size=3,
                kind="image",
                data=b"png",
                sha256="1" * 64,
            )
        )
        job = m.Job(
            user_id=user_id,
            kind="connect",
            state="done",
            connection_id=own_conn.id,
            thread_id=thread.id,
            payload={},
            run_after=NOW,
        )
        s.add(job)
        await s.flush()
        s.add(m.JobEvent(job_id=job.id, seq=1, type="status", data={}))
        s.add(m.JobAnswer(job_id=job.id, block_id=new_id(), values="x"))
        s.add(
            m.JobResearchCheckpoint(
                job_id=job.id,
                user_id=user_id,
                connection_id=own_conn.id,
                revision=1,
                format_version=1,
                intent_key="a" * 64,
                updated_at=NOW,
                expires_at=NOW + timedelta(days=1),
                kek_id="k",
                wrapped_dek=b"x",
                nonce=b"x",
                ciphertext=b"x",
                aad_version=1,
            )
        )
        s.add(
            m.ActionProposalRow(
                user_id=user_id,
                connection_id=own_conn.id,
                proposal={},
                fields_hash="f" * 64,
                expires_at=NOW,
            )
        )
        s.add(
            m.LlmUsageRow(
                user_id=user_id,
                purpose="chat",
                model="mock",
                input_cache_miss=1,
                input_cache_hit=0,
                output=1,
                cost_usd=0,
            )
        )
        await s.commit()
        own_conn_id = own_conn.id
    await services.vault.put(user_id, own_conn_id, SecretKind.PASSWORD, SECRET)
    await services.vault.put(user_id, own_conn_id, SecretKind.SESSION, "cookie-FIXTURE")
    await services.vault.put(user_id, new_id(), SecretKind.TRANSIENT, "answer-FIXTURE")


async def _counts(sm: Sm, user_id: str) -> dict[str, int]:
    """Rows per table that belong to the user (by user_id, or through a job)."""
    out: dict[str, int] = {}
    async with sm() as s:
        for table in Base.metadata.sorted_tables:
            if "user_id" in table.c:
                n = await s.scalar(
                    select(func.count()).select_from(table).where(table.c.user_id == user_id)
                )
            elif table.name in {"job_events", "job_answers"}:
                n = await s.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.job_id.in_(select(m.Job.id).where(m.Job.user_id == user_id)))
                )
            else:
                continue
            out[table.name] = int(n or 0)
    return out


async def _shared(sm: Sm) -> dict[str, int]:
    async with sm() as s:
        return {
            name: int(
                await s.scalar(select(func.count()).select_from(Base.metadata.tables[name])) or 0
            )
            for name in ("adapter_specs", "login_presets", "universities")
        }


async def _run(sm: Sm, clock: FrozenClock, *argv: str) -> tuple[int, Console]:
    out = Console()
    code = await reset_account.run(argv, sessionmaker=sm, clock=clock, out=out)
    return code, out


@pytest.fixture
async def two_users(
    tx_sessionmaker: Sm, services: Services
) -> tuple[tuple[str, str], tuple[str, str]]:
    shared = "https://shared.example.org"
    a = await make_user(tx_sessionmaker, "alice@example.com")
    b = await make_user(tx_sessionmaker, "bob@example.com")
    await _seed(tx_sessionmaker, services, a, tag="a", shared_origin=shared)
    await _seed(tx_sessionmaker, services, b, tag="b", shared_origin=shared)
    return (a, "alice@example.com"), (b, "bob@example.com")


async def test_without_yes_only_the_counts_are_printed(
    tx_sessionmaker: Sm, clock: FrozenClock, two_users: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    (a, email), _ = two_users
    before = await _counts(tx_sessionmaker, a)
    code, out = await _run(tx_sessionmaker, clock, "--email", email)
    assert code == 0
    assert await _counts(tx_sessionmaker, a) == before  # nothing deleted, no marker written
    assert "Будет удалено" in out.text
    assert "attachments: 1" in out.text
    assert "schedule_events: 1" in out.text
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(m.AuditLog)) == 0


async def test_one_account_is_cleared_and_the_other_is_untouched(
    tx_sessionmaker: Sm,
    clock: FrozenClock,
    services: Services,
    two_users: tuple[tuple[str, str], tuple[str, str]],
) -> None:
    (a, email), (b, _) = two_users
    b_before, a_before = await _counts(tx_sessionmaker, b), await _counts(tx_sessionmaker, a)
    assert a_before["secrets"] == 3
    code, out = await _run(tx_sessionmaker, clock, "--email", email.upper(), "--yes")
    assert code == 0, out.text

    after = await _counts(tx_sessionmaker, a)
    assert {t: n for t, n in after.items() if n and t not in KEPT} == {}  # every table, found alone
    for table in (
        "spaces", "connections", "documents", "chunks", "facts", "chat_threads", "chat_messages",
        "attachments", "jobs", "job_events", "job_answers", "job_research_checkpoints",
        "action_proposals", "secrets", "schedule_events", "courses", "deadlines",
    ):  # fmt: skip
        assert a_before[table] > 0, table  # the seed really had it
        assert after[table] == 0, table
    assert await _counts(tx_sessionmaker, b) == b_before  # the other account is exactly as before
    assert not await services.vault.has(a, "any", SecretKind.PASSWORD)
    # the account stays, and so do usage and the audit trail
    async with tx_sessionmaker() as s:
        assert await s.get(m.User, a) is not None
        assert after["llm_usage"] == a_before["llm_usage"] == 1
        marker = (
            await s.execute(select(m.AuditLog).where(m.AuditLog.kind == "product_data_reset"))
        ).scalar_one()
    assert marker.user_id == a
    assert marker.created_at == clock.now()
    assert marker.detail["deleted"]["attachments"] == 1
    assert SECRET not in str(marker.detail)  # counts only
    assert "Готово" in out.text


async def test_it_is_idempotent(
    tx_sessionmaker: Sm, clock: FrozenClock, two_users: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    (a, email), (b, _) = two_users
    assert (await _run(tx_sessionmaker, clock, "--email", email, "--yes"))[0] == 0
    b_state, shared_state = await _counts(tx_sessionmaker, b), await _shared(tx_sessionmaker)
    code, out = await _run(tx_sessionmaker, clock, "--email", email, "--yes")
    assert code == 0
    assert "ничего" in out.text
    assert {
        t: n for t, n in (await _counts(tx_sessionmaker, a)).items() if n and t not in KEPT
    } == {}
    assert await _counts(tx_sessionmaker, b) == b_state
    assert await _shared(tx_sessionmaker) == shared_state  # shared data needs --shared


async def test_the_reset_marker_reaches_the_client_and_login_survives(
    new_client: Callable[[], httpx.AsyncClient],
    tx_sessionmaker: Sm,
    clock: FrozenClock,
) -> None:
    c = new_client()
    acc = await signup(c, tx_sessionmaker, clock)
    assert "x-botai-data-reset" not in (await c.get("/api/v1/me")).headers
    code, _ = await _run(tx_sessionmaker, clock, "--email", acc.email, "--yes")
    assert code == 0
    me = await c.get("/api/v1/me")
    assert me.status_code == 200  # still signed in
    assert me.headers["x-botai-data-reset"] == str(int(clock.now().timestamp() * 1000))


async def test_shared_data_goes_only_when_nobody_else_uses_it(
    tx_sessionmaker: Sm, clock: FrozenClock, two_users: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    (_, email), _ = two_users
    # recipes: a's site, the shared site, b's site, and one per document source (a's and b's)
    assert await _shared(tx_sessionmaker) == {
        "adapter_specs": 5,
        "login_presets": 3,
        "universities": 3,
    }
    code, out = await _run(tx_sessionmaker, clock, "--email", email, "--yes", "--shared")
    assert code == 0, out.text
    assert await _shared(tx_sessionmaker) == {
        "adapter_specs": 3,  # the shared site, b's site and b's document
        "login_presets": 2,
        "universities": 2,  # b's own and the common one
    }
    async with tx_sessionmaker() as s:
        sites = set(
            await s.scalars(
                select(m.AdapterSpecRow.origin).where(~m.AdapterSpecRow.origin.like("doc:%"))
            )
        )
        documents = list(
            await s.scalars(
                select(m.AdapterSpecRow.origin).where(m.AdapterSpecRow.origin.like("doc:%"))
            )
        )
        b_connections = set(
            await s.scalars(select(m.Connection.id).where(m.Connection.origin.like("%b.example%")))
        )
    assert sites == {"https://b.example.org", "https://shared.example.org"}
    assert [d.removeprefix("doc:") for d in documents] == list(b_connections)
    assert "universities: 1" in out.text


async def test_shared_counts_are_the_same_in_a_dry_run(
    tx_sessionmaker: Sm, clock: FrozenClock, two_users: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    (_, email), _ = two_users
    before = await _shared(tx_sessionmaker)
    _, dry = await _run(tx_sessionmaker, clock, "--email", email, "--shared")
    assert await _shared(tx_sessionmaker) == before
    _, real = await _run(tx_sessionmaker, clock, "--email", email, "--shared", "--yes")

    def section(out: Console) -> str:
        return out.text.split("Общее, чем больше никто не пользуется:")[1].split("Готово")[0]

    assert section(dry).strip() == section(real).strip()


async def test_an_unknown_account_is_refused(
    tx_sessionmaker: Sm, clock: FrozenClock, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = await _run(tx_sessionmaker, clock, "--email", "nobody@example.com", "--yes")
    assert code == 1
    assert "Пользователь не найден" in capsys.readouterr().err


async def test_active_jobs_are_reported(
    tx_sessionmaker: Sm, clock: FrozenClock, two_users: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    (a, email), _ = two_users
    async with tx_sessionmaker() as s:
        s.add(m.Job(user_id=a, kind="chat", state="running", payload={}, run_after=NOW))
        await s.commit()
    _, out = await _run(tx_sessionmaker, clock, "--email", email)
    assert "активных заданий 1" in out.text


# --- the tables are found in the schema ------------------------------------------------------


def test_every_table_of_the_schema_is_classified() -> None:
    owned, unclassified = reset_account.user_tables(Base.metadata, "u")
    assert unclassified == []
    names = {t.name for t in Base.metadata.sorted_tables}
    # a table is user data, kept on purpose, or shared: nothing is left to guess
    shared = reset_account.SHARED & names  # (site maps appear with their own stream)
    assert names == set(owned) | reset_account.PRESERVED | shared
    assert {"job_events", "job_answers"} <= set(owned)  # owned through their job, no user_id
    assert not (set(owned) & KEPT)


def test_a_new_table_is_user_data_when_it_has_a_user_or_belongs_to_one() -> None:
    meta = MetaData()
    from sqlalchemy import Column, ForeignKey, String, Table

    Table("users", meta, Column("id", String, primary_key=True))
    Table("memory", meta, Column("id", String, primary_key=True), Column("user_id", String))
    Table("memory_links", meta, Column("memory_id", String, ForeignKey("memory.id")))
    Table("mystery", meta, Column("id", String, primary_key=True))
    owned, unclassified = reset_account.user_tables(meta, "u")
    assert set(owned) == {"memory", "memory_links"}
    assert unclassified == ["mystery"]


async def test_an_unclassified_table_stops_the_command(
    tx_sessionmaker: Sm, clock: FrozenClock, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy import Column, String, Table

    await make_user(tx_sessionmaker, "alice@example.com")
    extra = Table("zz_unclassified", Base.metadata, Column("id", String, primary_key=True))
    try:
        code, _ = await _run(tx_sessionmaker, clock, "--email", "alice@example.com", "--yes")
    finally:
        Base.metadata.remove(extra)
    assert code == 1
    assert "zz_unclassified" in capsys.readouterr().err
    async with tx_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(m.AuditLog)) == 0
