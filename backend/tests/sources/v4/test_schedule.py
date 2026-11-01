"""Planning of the next background sync of a v4 source (phase 3.3, plan §3.3).

The arithmetic (interval, exponential backoff with a cap, failure counter) is tested directly; the
runner integration proves what a real `sync` writes into `sources.next_sync_at`: success plans the
configured interval and clears the failure counter, a failure backs off (and a later success resets
it), a disabled source cancels its plan, and probe/canary/repair never plan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from botai.core.clock import FrozenClock
from botai.sources.v4 import schedule, service
from botai.sources.v4.runner import SourceRunner
from botai.sources.v4.schemas import SourcePatch
from tests.fakeuni import api as fakeuni_api
from tests.sources.v4.support import Account
from tests.sources.v4.test_runner import SCHEDULE_SPEC, attach_playbook, make_source

pytestmark = pytest.mark.db
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
INTERVAL = 600.0


@pytest.fixture(autouse=True)
def _fakeuni_state() -> Any:
    fakeuni_api.reset()
    yield
    fakeuni_api.reset()


# --- arithmetic ---------------------------------------------------------------------------------


def test_success_plans_the_interval() -> None:
    planned = schedule.next_sync_at(NOW, ok=True, failures=0, interval_s=INTERVAL)
    assert planned == NOW + timedelta(seconds=INTERVAL)


def test_failure_backs_off_exponentially_with_a_cap() -> None:
    assert schedule.backoff_for(1) == timedelta(minutes=15)
    assert schedule.backoff_for(2) == timedelta(minutes=30)
    assert schedule.backoff_for(3) == timedelta(hours=1)
    assert schedule.backoff_for(4) == timedelta(hours=2)
    assert schedule.backoff_for(20) == schedule.SYNC_RETRY_CAP


def test_failure_count_is_read_from_last_error() -> None:
    assert schedule.failures_of(None) == 0
    assert schedule.failures_of({}) == 0
    assert schedule.failures_of({"sync_failures": 2}) == 2
    assert schedule.failures_of({"sync_failures": True}) == 0
    assert schedule.failures_of({"sync_failures": "3"}) == 0
    assert schedule.failures_of({"sync_failures": 10_000}) == schedule.MAX_FAILURE_COUNT


def test_only_planned_statuses_are_synced_in_the_background() -> None:
    assert schedule.is_planned_status("active")
    assert schedule.is_planned_status("stale")
    assert schedule.is_planned_status("error")
    assert schedule.is_planned_status("needs_reauth")
    for status in ("draft", "probing", "learning", "waiting_user", "disabled", "archived"):
        assert not schedule.is_planned_status(status)


def test_interval_override_wins_over_settings() -> None:
    assert schedule.sync_interval_s(override=900) == 900
    assert schedule.sync_interval_s(override=1) == 1  # never a zero/negative interval


# --- the real runner ----------------------------------------------------------------------------


def _runner(
    tx_sessionmaker: Any,
    account: Account,
    transport: Any,
    store: Any,
    cookies: dict[str, str],
) -> SourceRunner:
    return SourceRunner(
        tx_sessionmaker(),
        user_id=account.user_id,
        transport=transport,
        artifact_store=store,
        cookies=cookies,
        clock=FrozenClock(NOW),
        sync_interval_s=INTERVAL,
    )


async def _fresh(tx_sessionmaker: Any, account: Account, source_id: str) -> Any:
    async with tx_sessionmaker() as session:
        return await service.get_source(session, user_id=account.user_id, source_id=source_id)


async def test_a_successful_sync_plans_the_interval_and_clears_errors(
    tx_sessionmaker: Any,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
    artifact_store: Any,
) -> None:
    source = await make_source(tx_sessionmaker, account)
    await attach_playbook(tx_sessionmaker, account, source, spec=SCHEDULE_SPEC)
    runner = _runner(tx_sessionmaker, account, fakeuni_transport, artifact_store, fakeuni_cookies)

    report = await runner.sync(source.id)
    assert report.ok, report.errors

    fresh = await _fresh(tx_sessionmaker, account, source.id)
    assert fresh.status == "active"
    assert fresh.next_sync_at == NOW + timedelta(seconds=INTERVAL)
    assert fresh.last_error is None


async def test_a_failure_backs_off_and_a_success_resets_the_counter(
    tx_sessionmaker: Any,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
    artifact_store: Any,
) -> None:
    source = await make_source(tx_sessionmaker, account)
    await attach_playbook(tx_sessionmaker, account, source, spec=SCHEDULE_SPEC)
    runner = _runner(tx_sessionmaker, account, fakeuni_transport, artifact_store, fakeuni_cookies)
    fakeuni_api.STATE.fail_status = 503  # the university is down: a retryable failure

    first = await runner.sync(source.id)
    assert not first.ok
    fresh = await _fresh(tx_sessionmaker, account, source.id)
    assert fresh.status == "error"
    assert fresh.next_sync_at == NOW + timedelta(minutes=15)
    assert fresh.last_error is not None
    assert fresh.last_error["sync_failures"] == 1

    second = await runner.sync(source.id)
    assert not second.ok
    fresh = await _fresh(tx_sessionmaker, account, source.id)
    assert fresh.next_sync_at == NOW + timedelta(minutes=30)
    assert fresh.last_error["sync_failures"] == 2

    fakeuni_api.STATE.fail_status = None  # the retry worked: the backoff must not stick
    third = await runner.sync(source.id)
    assert third.ok, third.errors
    fresh = await _fresh(tx_sessionmaker, account, source.id)
    assert fresh.next_sync_at == NOW + timedelta(seconds=INTERVAL)
    assert fresh.last_error is None

    # A new failure after the success starts from the base delay again (counter was reset).
    fakeuni_api.STATE.fail_status = 503
    fourth = await runner.sync(source.id)
    assert not fourth.ok
    fresh = await _fresh(tx_sessionmaker, account, source.id)
    assert fresh.next_sync_at == NOW + timedelta(minutes=15)
    assert fresh.last_error["sync_failures"] == 1


async def test_an_auth_failure_backs_off_too(
    tx_sessionmaker: Any,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
    artifact_store: Any,
) -> None:
    source = await make_source(tx_sessionmaker, account)
    await attach_playbook(tx_sessionmaker, account, source, spec=SCHEDULE_SPEC)
    runner = _runner(tx_sessionmaker, account, fakeuni_transport, artifact_store, {})  # no session

    report = await runner.sync(source.id)
    assert not report.ok
    fresh = await _fresh(tx_sessionmaker, account, source.id)
    assert fresh.status == "needs_reauth"
    assert fresh.next_sync_at == NOW + timedelta(minutes=15)  # one retry before giving up on time


async def test_probe_and_canary_do_not_plan_a_sync(
    tx_sessionmaker: Any,
    account: Account,
    fakeuni_transport: Any,
    fakeuni_cookies: dict[str, str],
    artifact_store: Any,
) -> None:
    source = await make_source(tx_sessionmaker, account)
    await attach_playbook(tx_sessionmaker, account, source, spec=SCHEDULE_SPEC)
    runner = _runner(tx_sessionmaker, account, fakeuni_transport, artifact_store, fakeuni_cookies)

    await runner.probe(source.id, persist=False)
    await runner.canary(source.id)
    fresh = await _fresh(tx_sessionmaker, account, source.id)
    assert fresh.next_sync_at is None


async def test_switching_a_source_off_cancels_its_plan(
    tx_sessionmaker: Any, account: Account
) -> None:
    source = await make_source(tx_sessionmaker, account)
    async with tx_sessionmaker() as session:
        fresh = await service.update_source(
            session,
            user_id=account.user_id,
            source_id=source.id,
            patch=SourcePatch(last_sync_at=NOW),
        )
        fresh.next_sync_at = NOW + timedelta(hours=1)
        await session.commit()

    async with tx_sessionmaker() as session:
        await service.disable_source(session, user_id=account.user_id, source_id=source.id)
        await session.commit()

    fresh = await _fresh(tx_sessionmaker, account, source.id)
    assert fresh.status == "disabled"
    assert fresh.next_sync_at is None

    async with tx_sessionmaker() as session:
        await service.set_status(
            session, user_id=account.user_id, source_id=source.id, status="archived"
        )
        await session.commit()
    fresh = await _fresh(tx_sessionmaker, account, source.id)
    assert fresh.next_sync_at is None
