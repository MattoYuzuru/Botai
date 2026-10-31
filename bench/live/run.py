"""Run the live benchmark of the BotAI v4 free harness (L3): login, session, API, counts, actions.

From the repository root::

    python -m bench.live.run --scenario all
    python -m bench.live.run --scenario hse-login --headed
    python -m bench.live.run --dry-run

The command re-executes itself with the backend virtualenv Python when Playwright is not
importable, so ``python`` can stay the system one. Primary credentials come from the macOS
Keychain through ``bench.live.secret_broker``; they are typed into the browser by this code and
never printed, logged or written to an artifact. Raw evidence is written outside the public
repository; a sanitized summary goes to ``docs/v4/_evidence/benchmark/<date>/``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python"


def _ensure_playwright() -> None:
    """Re-exec with the backend virtualenv when Playwright is missing in the current Python."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        if BACKEND_PYTHON.exists() and Path(sys.executable).resolve() != BACKEND_PYTHON.resolve():
            os.execv(
                str(BACKEND_PYTHON),
                [str(BACKEND_PYTHON), "-m", "bench.live.run", *sys.argv[1:]],
            )
        print(
            "playwright is not installed in this Python and backend/.venv was not found; "
            "run `cd backend && uv sync` first",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def _load_modules() -> tuple[Any, ...]:
    from . import __version__, report
    from .browser import BrowserConfig, find_chrome
    from .paths import STATE_ROOT, RunDir, evidence_dir, iso_now, make_run_dir
    from .redact import Redactor, sanitize_har
    from .scenarios import (
        scenario_cu_tasks,
        scenario_hse_login,
        scenario_hse_session,
    )
    from .secret_broker import SECRETS, cu_state_status, has_secret

    return (
        __version__,
        report,
        BrowserConfig,
        find_chrome,
        STATE_ROOT,
        RunDir,
        evidence_dir,
        iso_now,
        make_run_dir,
        Redactor,
        sanitize_har,
        scenario_hse_login,
        scenario_hse_session,
        scenario_cu_tasks,
        SECRETS,
        cu_state_status,
        has_secret,
    )


SCENARIO_REGISTRY = {
    "hse-login": {
        "title": "ВШЭ: живой вход по кредам из Keychain (новый профиль Chrome)",
        "needs_chrome_copy": False,
        "needs_keychain": True,
    },
    "hse-session": {
        "title": "ВШЭ: чтение на сессии, извлечённой из Chrome (без пароля)",
        "needs_chrome_copy": True,
        "needs_keychain": False,
    },
    "cu-tasks": {
        "title": "ЦУ: Actual Student Tasks на живой сессии из Chrome",
        "needs_chrome_copy": True,
        "needs_keychain": False,
    },
}
ALL_SCENARIOS = tuple(SCENARIO_REGISTRY)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bench.live.run",
        description=(
            "Live L3 benchmark: secret credentials from the macOS Keychain, real Chrome, "
            "artifacts outside the repository. Values of secrets are never printed."
        ),
    )
    parser.add_argument(
        "--scenario",
        default="all",
        help="comma-separated scenario ids or `all` (hse-login, hse-session, cu-tasks)",
    )
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--dry-run", action="store_true", help="check prerequisites, open nothing")
    parser.add_argument("--headed", action="store_true", help="show the real Chrome window")
    parser.add_argument(
        "--chromium",
        action="store_true",
        help="force bundled Playwright Chromium instead of the real Chrome",
    )
    parser.add_argument("--date", default=None, help="run date for directories (default: today)")
    parser.add_argument(
        "--browser-timeout-ms", type=int, default=90_000, help="navigation/login timeout per step"
    )
    return parser


SCENARIO_ALIASES = {
    "login": "hse-login",
    "hse": "hse-login",
    "session": "hse-session",
    "cu": "cu-tasks",
    "tasks": "cu-tasks",
}


def selected_scenarios(raw: str) -> list[str]:
    if raw.strip() in {"", "all"}:
        return list(ALL_SCENARIOS)
    requested = [
        SCENARIO_ALIASES.get(part.strip(), part.strip()) for part in raw.split(",") if part.strip()
    ]
    unknown = [part for part in requested if part not in SCENARIO_REGISTRY]
    if unknown:
        raise SystemExit(
            f"unknown scenario(s): {', '.join(unknown)}; known: {', '.join(ALL_SCENARIOS)}"
        )
    return requested


def dry_run(modules: tuple[Any, ...], scenarios: list[str], config: Any) -> int:
    (
        version,
        _report,
        _browser_config,
        find_chrome,
        state_root,
        _run_dir,
        _evidence_dir,
        _iso_now,
        _make_run_dir,
        redactor,
        _sanitize_har,
        _a,
        _b,
        _c,
        secrets,
        cu_state_status,
        has_secret,
    ) = modules
    chrome = find_chrome()
    plan = {
        "benchmarkVersion": version,
        "dryRun": True,
        "stateRoot": str(state_root),
        "browserMode": config.resolved_mode,
        "chrome": str(chrome) if chrome else None,
        "scenarios": [
            {
                "id": scenario,
                "title": SCENARIO_REGISTRY[scenario]["title"],
                "prerequisites": {
                    "chrome": bool(chrome),
                    "keychain": {name: has_secret(name) for name in secrets}
                    if SCENARIO_REGISTRY[scenario]["needs_keychain"]
                    else "not needed",
                },
            }
            for scenario in scenarios
        ],
        "cuSession": cu_state_status(),
        "policy": (
            "primary secrets: macOS Keychain only; values never printed; raw artifacts outside "
            "the repository; sanitized summary under docs/v4/_evidence/benchmark/"
        ),
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    ok = all(
        (not SCENARIO_REGISTRY[scenario]["needs_keychain"] or has_secret("hse_mail"))
        and (not SCENARIO_REGISTRY[scenario]["needs_chrome_copy"] or bool(chrome))
        for scenario in scenarios
    )
    return 0 if ok else 1


def make_config(modules: tuple[Any, ...], args: argparse.Namespace) -> Any:
    BrowserConfig = modules[2]
    return BrowserConfig(
        mode="chromium" if args.chromium else "auto",
        headless=not args.headed,
        navigation_timeout_ms=args.browser_timeout_ms,
    )


def _print_scenario_line(scenario: dict[str, Any]) -> None:
    barriers = scenario.get("barriers") or []
    codes = ",".join(str(item.get("code")) for item in barriers) or "-"
    print(
        f"  -> {scenario.get('status')} in {scenario.get('durationSec')}s (barriers: {codes})",
        flush=True,
    )


def _summary_table(scenarios: list[dict[str, Any]]) -> str:
    lines = [f"{'scenario':<14} {'status':<9} {'time':>8}  notes"]
    for scenario in scenarios:
        metrics = scenario.get("metrics") or {}
        notes: list[str] = []
        if "loginDurationSec" in metrics:
            notes.append(f"login={metrics['loginDurationSec']}s")
        if "scheduleRecords" in metrics:
            notes.append(f"scheduleRecords={metrics['scheduleRecords']}")
        if "apiEndpointCount" in metrics:
            notes.append(f"apiEndpoints={metrics['apiEndpointCount']}")
        if "taskRecords" in metrics:
            notes.append(f"taskRecords={metrics['taskRecords']}")
        if "safeGet" in metrics:
            notes.append(f"safeGet={metrics['safeGet'].get('status')}")
        barriers = scenario.get("barriers") or []
        if barriers:
            notes.append("barriers=" + ",".join(str(item.get("code")) for item in barriers))
        lines.append(
            f"{scenario.get('id')!s:<14} {scenario.get('status')!s:<9} "
            f"{scenario.get('durationSec'):>8}s  {'; '.join(notes)}"
        )
    return "\n".join(lines)


def final_secret_sweep(run: Any, redactor: Any) -> dict[str, int]:
    """Last line of defence: no registered secret value survives anywhere in the run directory."""
    files_scanned = 0
    redacted_files: list[str] = []
    for path in run.root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > 8_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files_scanned += 1
        if redactor.contains_secret(text):
            path.write_text(redactor.redact(text), encoding="utf-8")
            redacted_files.append(str(path.relative_to(run.root)))
    return {
        "filesScanned": files_scanned,
        "filesRedacted": len(redacted_files),
        "redactedFiles": redacted_files,
    }


def sanitize_har_files(run: Any, sanitize_har: Any, redactor: Any) -> int:
    count = 0
    for har in run.raw.rglob("*.har"):
        try:
            sanitize_har(har, redactor)
            har.chmod(0o600)
            count += 1
        except Exception:
            continue
    return count


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        for scenario, meta in SCENARIO_REGISTRY.items():
            print(f"{scenario:<14} {meta['title']}")
        return 0

    _ensure_playwright()
    modules = _load_modules()
    (
        version,
        report,
        BrowserConfig,
        _find_chrome,
        state_root,
        _run_dir,
        _evidence_dir,
        _iso_now,
        make_run_dir,
        Redactor,
        sanitize_har,
        scenario_hse_login,
        scenario_hse_session,
        scenario_cu_tasks,
        _secrets,
        _cu_state_status,
        _has_secret,
    ) = modules
    scenarios = selected_scenarios(args.scenario)
    config = make_config(modules, args)
    if args.dry_run:
        return dry_run(modules, scenarios, config)

    functions = {
        "hse-login": scenario_hse_login,
        "hse-session": scenario_hse_session,
        "cu-tasks": scenario_cu_tasks,
    }
    redactor = Redactor()
    run = make_run_dir(date=args.date)
    started_at = _iso_now()
    print(f"== BotAI live benchmark {version} | run {run.run_id}", flush=True)
    print(f"== raw artifacts: {run.root}", flush=True)
    scenario_dicts: list[dict[str, Any]] = []
    for scenario_id in scenarios:
        print(f"== scenario {scenario_id}: {SCENARIO_REGISTRY[scenario_id]['title']}", flush=True)
        try:
            scenario = functions[scenario_id](run, redactor, config)
        except Exception as exc:
            from .scenarios import ScenarioResult

            scenario = ScenarioResult(id=scenario_id, title=SCENARIO_REGISTRY[scenario_id]["title"])
            scenario.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            scenario.barrier("scenario_crash", scenario.error)
            scenario.finish("failed")
            print(traceback.format_exc(limit=3), file=sys.stderr)
        scenario_dicts.append(scenario.as_dict())
        _print_scenario_line(scenario_dicts[-1])

    sanitized_hars = sanitize_har_files(run, sanitize_har, redactor)
    results = report.build_results(
        run,
        scenarios=scenario_dicts,
        config={
            "mode": config.mode,
            "resolvedMode": config.resolved_mode,
            "loginScenarioMode": "chrome-fresh (real Chrome, empty profile)",
            "sessionScenarioMode": "chrome-copy (real Chrome, copied owner profile)",
            "headless": config.headless,
            "locale": config.locale,
            "timezoneId": config.timezone_id,
        },
        redactor=redactor,
        started_at=started_at,
    )
    results["harFilesSanitized"] = sanitized_hars
    sweep = final_secret_sweep(run, redactor)
    results["secretSweep"] = sweep
    results_path = report.write_results(run, results, redactor)
    report.write_run_summary(run, results)
    evidence_path = report.write_evidence(run, results)

    print()
    print(_summary_table(scenario_dicts))
    print()
    print(report.run_header_line(results))
    print(f"results:  {results_path}")
    print(f"evidence: {evidence_path}  (sanitized, may be committed)")
    print(
        f"secrets:  {redactor.registeredCount} registered values; sweep "
        f"scanned {sweep['filesScanned']} files, redacted {sweep['filesRedacted']}"
    )
    failed = results["totals"].get("failed", 0)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
