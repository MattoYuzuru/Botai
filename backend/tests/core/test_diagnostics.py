"""Operator diagnostics remain bounded, useful and safe in either switch state."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import json
import os
import sys
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from botai.core import diagnostics as d
from botai.settings import Settings

JOB_A = "01M61YP9N7N1HHQRCCVJRGS74K"
JOB_B = "01M61ZC5X9X7J9TMA3G5VZ519P"
THREAD = "01M61JYBYEVF5H75AQB8YB69F4"
MARKER = "private-student-password-never-retain"


@pytest.fixture
def traces(tmp_path: Path) -> Generator[Path]:
    d.configure_diagnostics(
        Settings(
            botai_diagnostics=True,
            botai_diagnostics_dir=str(tmp_path),
            botai_diagnostics_max_bytes=1024,
        )
    )
    try:
        yield tmp_path
    finally:
        d.configure_diagnostics(
            Settings(botai_diagnostics=False, botai_diagnostics_dir=str(tmp_path))
        )


def records(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in directory.glob("*.jsonl*")
        if not path.is_symlink()
        for line in path.read_text().splitlines()
    ]


def test_off_switch_creates_no_directory_and_stops_existing_writer(tmp_path: Path) -> None:
    directory = tmp_path / "disabled"
    cfg = Settings(botai_diagnostics=False, botai_diagnostics_dir=str(directory))
    d.configure_diagnostics(cfg)
    d.diagnostic_event("job.start", job_id=JOB_A)
    assert not directory.exists()
    d.configure_diagnostics(cfg.model_copy(update={"botai_diagnostics": True}))
    d.diagnostic_event("job.start", job_id=JOB_A)
    before = records(directory)
    d.configure_diagnostics(cfg)
    d.diagnostic_event("job.complete", job_id=JOB_A)
    assert records(directory) == before


def test_only_structural_facts_and_valid_identifiers_are_persisted(traces: Path) -> None:
    with d.diagnostic_scope(thread_id=THREAD, job_id=JOB_A):
        d.diagnostic_event(
            "browser.snapshot",
            status=200,
            auth_required=True,
            password=MARKER,
            cookie=MARKER,
            body=MARKER,
            reasoning=MARKER,
            reason=MARKER,
            connection_id=MARKER,
            input_chars=10**1000,
            url=f"https://{MARKER}.test/private",
        )
    row = records(traces)[0]
    assert row["job_id"] == JOB_A
    assert row["thread_id"] == THREAD
    assert row["auth_required"] is True
    assert row["status"] == 200
    assert MARKER not in json.dumps(row)
    assert "body" not in row
    assert "input_chars" not in row
    assert next(traces.glob("*.jsonl")).stat().st_mode & 0o777 == 0o600


async def test_concurrent_jobs_do_not_exchange_correlation(traces: Path) -> None:
    async def run(job: str) -> None:
        with d.diagnostic_scope(job_id=job):
            await asyncio.sleep(0)
            d.diagnostic_event("job.start", thread_id=THREAD)

    await asyncio.gather(run(JOB_A), run(JOB_B))
    d.diagnostic_event("job.exit")
    rows = records(traces)
    assert {row["job_id"] for row in rows if row["event"] == "job.start"} == {JOB_A, JOB_B}
    assert "job_id" not in next(row for row in rows if row["event"] == "job.exit")


def test_replay_binding_vocabulary_accepts_only_static_modes(traces: Path) -> None:
    d.diagnostic_event("canary.candidate", auth_type="bearer", transport="http", rendered=False)
    d.diagnostic_event("canary.candidate", auth_type=MARKER, transport=MARKER, rendered=MARKER)
    first, second = records(traces)
    assert first["auth_type"] == "bearer"
    assert first["transport"] == "http"
    assert first["rendered"] is False
    assert not {"auth_type", "transport", "rendered"} & second.keys()
    assert MARKER not in json.dumps(records(traces))


def test_urls_are_correlatable_without_path_query_host_or_credentials(traces: Path) -> None:
    url = f"https://login:{MARKER}@{MARKER}.test/students/{MARKER}?token={MARKER}#private"
    facts = d.url_facts(url)
    assert facts == d.url_facts(url)
    assert facts["path_depth"] == 2
    assert facts["query_count"] == 1
    d.diagnostic_event("observe.page", **facts)
    assert MARKER not in json.dumps(records(traces))
    assert d.url_facts("https://[invalid") == {}


def test_size_rotation_enforces_total_service_budget(traces: Path) -> None:
    for _ in range(40):
        d.diagnostic_event(
            "browser.request", job_id=JOB_A, **d.url_facts("https://fakeuni.test/path")
        )
    files = list(traces.glob("*.jsonl*"))
    assert len(files) <= 3
    assert sum(path.stat().st_size for path in files) <= 3 * 1024
    assert records(traces)


def test_retention_keeps_low_volume_hours_but_expires_old_files(traces: Path) -> None:
    now = datetime.now(UTC)
    paths: list[Path] = []
    for hours in (1, 2, 3, 4, 25):
        path = traces / f"worker-{now - timedelta(hours=hours):%Y%m%d%H}.jsonl"
        path.write_text("{}\n")
        paths.append(path)
    unrelated = traces / "do-not-delete.txt"
    unrelated.write_text(MARKER)
    d.purge_diagnostics()
    assert all(path.exists() for path in paths[:4])
    assert not paths[-1].exists()
    assert unrelated.read_text() == MARKER


def test_idle_writer_does_not_pin_expired_records(
    traces: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d.diagnostic_event("job.start", job_id=JOB_A)
    future = datetime.now(UTC) + timedelta(hours=25)

    class Later(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return future

    monkeypatch.setattr(d, "datetime", Later)
    d.purge_diagnostics()
    assert not list(traces.glob("*.jsonl*"))
    d.diagnostic_event("job.complete", job_id=JOB_A)
    assert len(records(traces)) == 1


def test_event_budget_emits_one_explicit_truncation(
    traces: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(d, "MAX_EVENTS_PER_SCOPE", 2)
    for _ in range(5):
        d.diagnostic_event("job.status", job_id=JOB_A, state="running")
    rows = records(traces)
    assert len(rows) == 3
    assert sum(row["event"] == "diagnostics.truncated" for row in rows) == 1


def test_unwritable_sink_does_not_break_the_job(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    d.configure_diagnostics(
        Settings(botai_diagnostics=True, botai_diagnostics_dir=str(blocker / "traces"))
    )
    try:
        d.diagnostic_event("job.start", job_id=JOB_A)
    finally:
        d.configure_diagnostics(Settings(botai_diagnostics=False))


def test_export_filters_thread_and_never_follows_links(
    traces: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    d.diagnostic_event("job.start", job_id=JOB_A, thread_id=THREAD)
    d.diagnostic_event("job.start", job_id=JOB_B)
    secret = traces / "private.txt"
    secret.write_text(MARKER)
    os.symlink(secret, traces / "worker-2026102800.jsonl")
    monkeypatch.setattr(
        d,
        "get_settings",
        lambda: Settings(botai_diagnostics=True, botai_diagnostics_dir=str(traces)),
    )
    monkeypatch.setattr(sys, "argv", ["diagnostics", "--thread", THREAD])
    d.main()
    out = capsys.readouterr().out
    assert JOB_A in out
    assert JOB_B not in out
    assert MARKER not in out


def test_all_instrumentation_fields_have_a_declared_sink_type() -> None:
    import ast

    known = (
        d._IDS
        | d._NUMBERS
        | d._BOOLEANS
        | set(d._CODES)
        | {"host_hash", "url_hash", "path_hash", "grant_hash"}
    )  # pyright: ignore[reportPrivateUsage]
    root = Path(__file__).resolve().parents[2] / "src/botai"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "diagnostic_event"
            ):
                assert {key.arg for key in node.keywords if key.arg} <= known, path


def test_thread_export_joins_proxy_decisions_by_verified_grant(
    traces: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    grant = "a" * 16
    d.diagnostic_event("egress.grant", thread_id=THREAD, job_id=JOB_A, grant_hash=grant)
    d.diagnostic_event(
        "egress.decision", grant_hash=grant, decision="deny", reason="not_allowlisted", status=403
    )
    d.diagnostic_event(
        "egress.decision", grant_hash="b" * 16, decision="allow", reason="ok", status=200
    )
    monkeypatch.setattr(
        d,
        "get_settings",
        lambda: Settings(botai_diagnostics=True, botai_diagnostics_dir=str(traces)),
    )
    monkeypatch.setattr(sys, "argv", ["diagnostics", "--thread", THREAD, "--summary"])
    d.main()
    out = json.loads(capsys.readouterr().out)
    assert out["record_count"] == 2
    assert out["events"]["egress.decision"] == 1


async def test_unhandled_http_failure_has_safe_correlated_trace(
    traces: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    import botai.main as app_module

    cfg = Settings(botai_diagnostics=True, botai_diagnostics_dir=str(traces))
    monkeypatch.setattr(d, "get_settings", lambda: cfg)
    monkeypatch.setattr(app_module, "get_settings", lambda: cfg)
    app = app_module.create_app()

    @app.get("/fixture-crash")
    async def crash() -> None:
        raise RuntimeError(MARKER)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/fixture-crash")
    assert response.status_code == 500
    row = next(row for row in records(traces) if row["event"] == "http.failed")
    assert row["error_code"] == "internal_error"
    assert "request_id" in row
    assert MARKER not in json.dumps(records(traces))
