"""Feedback reports typed execution facts without echoing source bodies or error text."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from botai.core.vault import InMemoryVault
from botai.learn import canary
from botai.sources.adapter import AdapterResult, EndpointOutcome, SessionExpired, SyncContext
from tests.sources.recipes import recipe_spec, timetable_endpoint


class Adapter:
    def __init__(self, value: AdapterResult | Exception) -> None:
        self.value = value

    async def fetch(self, _ctx: SyncContext) -> AdapterResult:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def context() -> SyncContext:
    return SyncContext(
        "user",
        "connection",
        "https://fakeuni.test",
        ("fakeuni.test",),
        datetime.now(UTC),
        InMemoryVault(),
    )


async def test_typed_auth_refusal_does_not_echo_private_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = SessionExpired("private-session-marker", code="auth_rejected", status=403)
    monkeypatch.setattr(canary.AdapterFactory, "create", lambda *_: Adapter(failure))
    spec = recipe_spec([timetable_endpoint()])
    with pytest.raises(SessionExpired):
        await canary.run_canary(spec, context(), require=["scheduleEvents"])
    report = await canary.run_canary(
        spec, context(), require=["scheduleEvents"], capture_failures=True
    )
    assert not report.ok
    assert report.findings[0].code == "auth_rejected"
    assert report.findings[0].status == 403
    assert "private-session-marker" not in str(report)
    assert "private-session-marker" not in "\n".join(report.feedback())


@pytest.mark.parametrize("problem", ["limit", "no_progress", "unsupported_capability"])
async def test_incomplete_execution_never_becomes_a_successful_empty_canary(
    monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    ctx = context()
    result = AdapterResult(
        [],
        ctx.now,
        outcomes=[EndpointOutcome("calendar", "scheduleEvents", False, problem=problem)],
    )
    monkeypatch.setattr(canary.AdapterFactory, "create", lambda *_: Adapter(result))
    report = await canary.run_canary(
        recipe_spec([timetable_endpoint()]), ctx, require=["scheduleEvents"], capture_failures=True
    )
    assert not report.ok
    assert problem in {finding.code for finding in report.findings}
    assert result.complete_kinds() == set()
