"""Quotas and token accounting (FR-024, NFR-008, security.md T7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from botai.agent.llm import DbUsageGate
from botai.agent.llm import is_peak as agent_is_peak
from botai.core.clock import FrozenClock
from botai.core.errors import BotaiError, ErrorCode
from botai.core.ids import new_id
from botai.core.llm import LlmUsage
from botai.db.models import LlmUsageRow
from botai.settings import Settings
from botai.usage import service
from tests.auth.support import NOW, Sm, make_user, signup

pytestmark = pytest.mark.db


def usage(miss: int = 100, hit: int = 0, out: int = 50, model: str = "deepseek-flash") -> LlmUsage:
    return LlmUsage(
        input_cache_miss=miss, input_cache_hit=hit, output=out, model=model, latency_ms=900
    )


def limits(tokens: int = 1_000, per_minute: int = 5) -> Settings:
    return Settings(env="test", quota_tokens_per_day=tokens, quota_requests_per_minute=per_minute)  # type: ignore[call-arg]


async def spend(
    sm: Sm, user_id: str, at: datetime, tokens: int = 100, job_id: str | None = None
) -> None:
    async with sm() as s:
        await service.record_usage(
            s,
            user_id=user_id,
            job_id=job_id,
            purpose="agent",
            usage=usage(tokens, 0, 0),
            peak=False,
            now=at,
        )


# --- pricing ----------------------------------------------------------------------------------


def test_cost_per_million_tokens_and_peak_doubling() -> None:
    u = usage(miss=1_000_000, hit=1_000_000, out=1_000_000)
    assert service.cost_usd(u, peak=False) == pytest.approx(0.15 + 0.003 + 0.60)
    assert service.cost_usd(u, peak=True) == pytest.approx(2 * (0.15 + 0.003 + 0.60))
    pro = usage(miss=1_000_000, hit=0, out=1_000_000, model="deepseek-v4-pro")
    assert service.cost_usd(pro, peak=False) == pytest.approx(0.66 + 1.98)
    unknown = usage(miss=1_000_000, hit=0, out=0, model="something-new")
    assert service.cost_usd(unknown, peak=False) == pytest.approx(0.15)  # priced as the cheap model
    assert service.cost_usd(LlmUsage(), peak=False) == 0.0


@pytest.mark.parametrize(
    ("when", "peak"),
    [
        ("2026-10-26T00:59:59+00:00", False),  # Monday
        ("2026-10-26T01:00:00+00:00", True),
        ("2026-10-26T03:59:59+00:00", True),
        ("2026-10-26T04:00:00+00:00", False),
        ("2026-10-26T05:59:59+00:00", False),
        ("2026-10-26T06:00:00+00:00", True),
        ("2026-10-26T09:59:59+00:00", True),
        ("2026-10-26T10:00:00+00:00", False),
        ("2026-10-26T20:00:00+00:00", False),
        ("2026-10-30T09:00:00+00:00", True),  # Friday
        ("2026-10-31T09:00:00+00:00", False),  # Saturday
        ("2026-11-01T02:00:00+00:00", False),  # Sunday
        ("2026-10-26T04:30:00+03:00", True),  # 01:30 UTC given in Moscow time
    ],
)
def test_peak_window_is_utc_weekdays(when: str, peak: bool) -> None:
    at = datetime.fromisoformat(when)
    assert service.is_peak(at) is peak
    assert agent_is_peak(at) is peak  # W5's provider and the accounting agree


# --- record_usage -------------------------------------------------------------------------------


async def test_record_usage_writes_one_priced_row(tx_sessionmaker: Sm) -> None:
    uid = await make_user(tx_sessionmaker)
    job = new_id()
    u = usage(miss=2_000, hit=8_000, out=500)
    async with tx_sessionmaker() as s:
        await service.record_usage(
            s, user_id=uid, job_id=job, purpose="learn", usage=u, peak=False, now=NOW
        )
        await service.record_usage(
            s, user_id=uid, job_id=None, purpose="agent", usage=u, peak=True, now=NOW
        )
    async with tx_sessionmaker() as s:
        rows = (await s.scalars(select(LlmUsageRow).order_by(LlmUsageRow.purpose))).all()
    chat, learn = rows
    assert (learn.user_id, learn.job_id, learn.purpose, learn.model) == (
        uid,
        job,
        "learn",
        "deepseek-flash",
    )
    assert (learn.input_cache_miss, learn.input_cache_hit, learn.output, learn.latency_ms) == (
        2_000,
        8_000,
        500,
        900,
    )
    assert float(learn.cost_usd) == pytest.approx(service.cost_usd(u, peak=False), abs=1e-6)
    assert float(chat.cost_usd) == pytest.approx(2 * float(learn.cost_usd), abs=1e-6)
    assert chat.job_id is None
    assert learn.created_at == NOW


async def test_record_usage_survives_a_rollback_of_the_callers_work(engine: Any) -> None:
    """The money is spent whether or not the surrounding work succeeds: the row is committed."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    uid = await make_user(sm)
    try:
        async with sm() as s:
            await service.record_usage(
                s, user_id=uid, job_id=None, purpose="agent", usage=usage(), peak=False
            )
            await s.rollback()  # whatever the caller does next
        async with sm() as s:
            assert (
                await s.scalar(select(LlmUsageRow.id).where(LlmUsageRow.user_id == uid)) is not None
            )
    finally:
        async with sm() as s:
            await s.execute(text("delete from users where id = :u"), {"u": uid})
            await s.commit()


async def test_record_usage_stores_counters_only(tx_sessionmaker: Sm) -> None:
    uid = await make_user(tx_sessionmaker)
    async with tx_sessionmaker() as s:
        await service.record_usage(
            s, user_id=uid, job_id=None, purpose="x" * 40, usage=usage(model="m" * 200), peak=False
        )
        row = (await s.scalars(select(LlmUsageRow))).one()
    assert len(row.purpose) <= 12
    assert len(row.model) <= 60
    columns = {c.name for c in LlmUsageRow.__table__.columns}
    assert not columns & {"prompt", "messages", "content", "answer", "text"}


# --- check_quota --------------------------------------------------------------------------------


async def test_a_user_under_the_daily_budget_passes(tx_sessionmaker: Sm) -> None:
    uid = await make_user(tx_sessionmaker)
    await spend(tx_sessionmaker, uid, NOW, tokens=999)
    async with tx_sessionmaker() as s:
        await service.check_quota(s, uid, settings=limits(1_000), now=NOW)


async def test_the_daily_budget_stops_the_user_with_the_reset_time(tx_sessionmaker: Sm) -> None:
    uid = await make_user(tx_sessionmaker)
    await spend(tx_sessionmaker, uid, NOW, tokens=700)
    await spend(tx_sessionmaker, uid, NOW + timedelta(seconds=1), tokens=300)
    async with tx_sessionmaker() as s:
        with pytest.raises(BotaiError) as err:
            await service.check_quota(
                s, uid, settings=limits(1_000), now=NOW + timedelta(minutes=5)
            )
    e = err.value
    assert e.code == ErrorCode.QUOTA_EXCEEDED
    assert e.status == 429
    assert e.extra["scope"] == "daily"
    assert e.extra["limit"] == 1_000
    assert e.extra["used"] == 1_000
    assert e.extra["resetsAt"] == "2026-10-27T21:00:00Z"  # local midnight in Moscow (UTC+3)
    assert "Суточный лимит" in e.detail


async def test_the_budget_is_per_user(tx_sessionmaker: Sm) -> None:
    greedy = await make_user(tx_sessionmaker)
    other = await make_user(tx_sessionmaker)
    await spend(tx_sessionmaker, greedy, NOW, tokens=5_000)
    async with tx_sessionmaker() as s:
        await service.check_quota(s, other, settings=limits(1_000), now=NOW)  # untouched
        with pytest.raises(BotaiError):
            await service.check_quota(s, greedy, settings=limits(1_000), now=NOW)


async def test_the_budget_resets_at_local_midnight(tx_sessionmaker: Sm) -> None:
    uid = await make_user(tx_sessionmaker)
    yesterday = datetime(2026, 10, 27, 20, 59, tzinfo=UTC)  # 23:59 in Moscow
    await spend(tx_sessionmaker, uid, yesterday, tokens=5_000)
    async with tx_sessionmaker() as s:
        with pytest.raises(BotaiError):
            await service.check_quota(
                s, uid, settings=limits(1_000), now=yesterday + timedelta(seconds=30)
            )
        # two minutes later it is a new day in Moscow
        await service.check_quota(
            s, uid, settings=limits(1_000), now=yesterday + timedelta(minutes=2)
        )


async def test_cache_hits_count_toward_the_budget_too(tx_sessionmaker: Sm) -> None:
    uid = await make_user(tx_sessionmaker)
    async with tx_sessionmaker() as s:
        await service.record_usage(
            s,
            user_id=uid,
            job_id=None,
            purpose="agent",
            usage=usage(10, 900, 90),
            peak=False,
            now=NOW,
        )
        with pytest.raises(BotaiError):
            await service.check_quota(
                s, uid, settings=limits(1_000), now=NOW + timedelta(minutes=2)
            )


async def test_the_request_rate_stops_a_runaway_loop(tx_sessionmaker: Sm) -> None:
    uid = await make_user(tx_sessionmaker)
    other = await make_user(tx_sessionmaker)
    cfg = limits(10_000_000, per_minute=3)
    for n in range(3):
        await spend(tx_sessionmaker, uid, NOW + timedelta(seconds=n * 10), tokens=1)
    await spend(tx_sessionmaker, other, NOW, tokens=1)
    now = NOW + timedelta(seconds=30)
    async with tx_sessionmaker() as s:
        with pytest.raises(BotaiError) as err:
            await service.check_quota(s, uid, settings=cfg, now=now)
        e = err.value
        assert e.code == ErrorCode.QUOTA_EXCEEDED
        assert e.status == 429
        assert e.extra["scope"] == "minute"
        assert e.extra["used"] == 3
        assert e.extra["limit"] == 3
        assert e.extra["resetsAt"] == "2026-10-27T12:01:00Z"  # the oldest request leaves the window
        assert "30 с" in e.detail
        await service.check_quota(
            s, other, settings=cfg, now=now
        )  # someone else's loop is not mine

        # a slot frees when the oldest request is a minute old
        await service.check_quota(s, uid, settings=cfg, now=NOW + timedelta(seconds=61))


async def test_a_slow_user_never_hits_the_rate_limit(tx_sessionmaker: Sm) -> None:
    uid = await make_user(tx_sessionmaker)
    cfg = limits(10_000_000, per_minute=3)
    async with tx_sessionmaker() as s:
        for n in range(20):
            at = NOW + timedelta(seconds=n * 25)
            await service.check_quota(s, uid, settings=cfg, now=at)
            await spend(tx_sessionmaker, uid, at, tokens=1)


async def test_the_problem_body_has_the_reset_time(tx_sessionmaker: Sm, app: Any) -> None:
    """What a client sees when a job or a request dies of the quota (contracts: quota_exceeded)."""
    uid = await make_user(tx_sessionmaker)
    await spend(tx_sessionmaker, uid, NOW, tokens=5_000)

    @app.get("/__quota")
    async def boom() -> None:
        async with tx_sessionmaker() as s:
            await service.check_quota(s, uid, settings=limits(1_000), now=NOW)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t") as c:
        r = await c.get("/__quota")
    assert r.status_code == 429
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["code"] == "quota_exceeded"
    assert body["resetsAt"] == "2026-10-27T21:00:00Z"
    assert body["scope"] == "daily"


# --- the provider's gate (W5) on the real service ---------------------------------------------------


async def test_the_providers_gate_records_and_enforces_through_this_service(
    tx_sessionmaker: Sm,
) -> None:
    uid = await make_user(tx_sessionmaker)
    gate = DbUsageGate(lambda: tx_sessionmaker)
    await gate.check(uid)  # fresh user: allowed (default limits)
    await gate.record(
        user_id=uid,
        job_id=None,
        purpose="agent",
        usage=usage(miss=Settings().quota_tokens_per_day + 1, out=0),
        peak=False,
    )
    with pytest.raises(BotaiError) as err:
        await gate.check(uid)
    assert err.value.code == ErrorCode.QUOTA_EXCEEDED


# --- the API --------------------------------------------------------------------------------------


async def test_usage_endpoint_reports_today_and_seven_days(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock, settings: Settings
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    bob = await make_user(tx_sessionmaker)
    day = lambda n: NOW - timedelta(days=n)  # noqa: E731
    await spend(tx_sessionmaker, acc.id, day(0), tokens=300)
    await spend(tx_sessionmaker, acc.id, day(0) + timedelta(seconds=1), tokens=200)
    await spend(tx_sessionmaker, acc.id, day(2), tokens=1_000)
    await spend(tx_sessionmaker, acc.id, day(30), tokens=9_999)  # outside the week
    await spend(tx_sessionmaker, bob, day(0), tokens=4_000)  # someone else's

    r = await client.get("/api/v1/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["tokensToday"] == 500
    assert body["tokensLimit"] == settings.quota_tokens_per_day
    assert body["resetsAt"].startswith("2026-10-28T00:00:00")  # Moscow midnight, +03:00
    days = body["lastDays"]
    assert [d["day"] for d in days] == [f"2026-10-{n}" for n in range(21, 28)]
    assert [d["tokens"] for d in days] == [0, 0, 0, 0, 1_000, 0, 500]
    assert days[-1]["costUsd"] == pytest.approx(500 * 0.15 / 1_000_000, abs=1e-6)


async def test_a_late_evening_request_belongs_to_the_next_moscow_day(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    clock.advance(timedelta(hours=10))  # 22:00 UTC = 01:00 on the 28th in Moscow
    await spend(
        tx_sessionmaker, acc.id, datetime(2026, 10, 27, 21, 30, tzinfo=UTC), tokens=70
    )  # 00:30 MSK
    await spend(
        tx_sessionmaker, acc.id, datetime(2026, 10, 27, 20, 30, tzinfo=UTC), tokens=30
    )  # 23:30 MSK
    body = (await client.get("/api/v1/usage")).json()
    assert body["tokensToday"] == 70
    assert body["lastDays"][-1] == {
        "day": "2026-10-28",
        "tokens": 70,
        "costUsd": body["lastDays"][-1]["costUsd"],
    }
    assert body["lastDays"][-2]["tokens"] == 30


async def test_me_reports_the_same_quota_as_usage(
    client: httpx.AsyncClient, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    acc = await signup(client, tx_sessionmaker, clock)
    await spend(tx_sessionmaker, acc.id, NOW, tokens=123)
    me = (await client.get("/api/v1/me")).json()["quota"]
    mine = (await client.get("/api/v1/usage")).json()
    assert (me["tokensToday"], me["tokensLimit"], me["resetsAt"]) == (
        mine["tokensToday"],
        mine["tokensLimit"],
        mine["resetsAt"],
    )
    assert me["tokensToday"] == 123


async def test_usage_needs_a_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/usage")).status_code == 401
    assert (await client.get("/api/v1/usage/pool")).status_code == 401


async def test_only_the_owner_sees_the_pool(
    new_client: Any, tx_sessionmaker: Sm, clock: FrozenClock
) -> None:
    student = new_client()
    alice = await signup(student, tx_sessionmaker, clock, email="alice@example.com")
    bob_client = new_client()
    bob = await signup(bob_client, tx_sessionmaker, clock, email="bob@example.com")
    await spend(tx_sessionmaker, alice.id, NOW, tokens=400)
    await spend(tx_sessionmaker, alice.id, NOW - timedelta(days=3), tokens=600)
    await spend(tx_sessionmaker, bob.id, NOW - timedelta(days=1), tokens=50)
    assert (await student.get("/api/v1/usage/pool")).status_code == 403

    from tests.auth.support import PASSWORD

    await make_user(tx_sessionmaker, "owner@example.com", is_owner=True, password=PASSWORD)
    owner = new_client()
    await owner.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
    )
    r = await owner.get("/api/v1/usage/pool")
    assert r.status_code == 200
    body = r.json()
    by_email = {u["email"]: u for u in body["users"]}
    assert by_email["alice@example.com"]["tokensToday"] == 400
    assert by_email["alice@example.com"]["tokensWeek"] == 1_000
    assert by_email["bob@example.com"]["tokensToday"] == 0
    assert by_email["bob@example.com"]["tokensWeek"] == 50
    assert by_email["owner@example.com"]["tokensWeek"] == 0  # quiet users are listed too
    assert body["tokensLimit"] > 0
    assert "passwordHash" not in r.text
