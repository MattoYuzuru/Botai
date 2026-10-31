"""Reports of a live run: full ``results.json`` outside the repo and a sanitized summary inside it.

The public repository gets only counts, statuses, endpoint templates and steps -- no cookies,
tokens, login e-mail or personal data. The full report also never contains the owner's primary
secrets: they are redacted by :class:`bench.live.redact.Redactor` before anything is written.
"""

from __future__ import annotations

import json
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .paths import EVIDENCE_ROOT, RunDir, iso_now
from .redact import Redactor, write_private_json

MAX_HOST_DETAIL = 20


def build_results(
    run: RunDir,
    *,
    scenarios: list[dict[str, Any]],
    config: dict[str, Any],
    redactor: Redactor,
    started_at: str,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """Assemble ``results.json``: run identity, host, scenarios, totals, secrets policy."""
    statuses = [scenario.get("status") for scenario in scenarios]
    results: dict[str, Any] = {
        "schemaVersion": 1,
        "benchmarkVersion": __version__,
        "runId": run.run_id,
        "date": run.date,
        "startedAt": started_at,
        "finishedAt": finished_at or iso_now(),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "browserConfig": config,
        },
        "credentials": {
            "policy": "primary secrets live in the macOS Keychain; the model and logs never see them",
            "registeredSecretValues": redactor.registeredCount,
            "visibleToModelOrLogs": False,
        },
        "scenarios": scenarios,
        "totals": {
            "scenarios": len(scenarios),
            "success": statuses.count("success"),
            "partial": statuses.count("partial"),
            "blocked": statuses.count("blocked"),
            "failed": statuses.count("failed"),
            "durationSec": round(sum(float(item.get("durationSec") or 0) for item in scenarios), 3),
        },
        "artifactRoot": str(run.root),
        "artifactsPolicy": (
            "raw session material (HAR, storage state, screenshots, cookies) stays outside the "
            "repository: ~/.local/state/botai-v4-bench/<date>/<run>/ (0700, files 0600)"
        ),
    }
    return redactor.redact_payload(results)


def sanitize_for_repository(results: dict[str, Any], run: RunDir) -> dict[str, Any]:
    """Copy of the results that may be committed: relative paths, no host home directories."""

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            text = value.replace(str(run.root), "<run>")
            text = text.replace(str(Path.home()), "~")
            text = text.replace(str(EVIDENCE_ROOT.parents[2]), "<repo>")
            return text
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {str(key): scrub(item) for key, item in value.items()}
        return value

    summary = scrub(results)
    summary["artifactRoot"] = f"~/.local/state/botai-v4-bench/{run.date}/{run.run_id}"
    summary["repositoryCopy"] = True
    summary["note"] = (
        "Sanitized copy for the public repository: statuses, counts, steps, endpoint templates; "
        "no cookies, tokens, login e-mail, personal data or absolute home paths."
    )
    return summary


def scenario_digest(results: dict[str, Any]) -> dict[str, Any]:
    """Compact per-scenario digest used in the evidence file and the docs."""
    digest: list[dict[str, Any]] = []
    for scenario in results.get("scenarios", []):
        metrics = scenario.get("metrics", {}) or {}
        steps = scenario.get("steps", []) or []
        digest.append(
            {
                "id": scenario.get("id"),
                "title": scenario.get("title"),
                "level": scenario.get("level"),
                "status": scenario.get("status"),
                "durationSec": scenario.get("durationSec"),
                "barriers": scenario.get("barriers", []),
                "steps": [
                    {
                        "name": step.get("name"),
                        "ok": step.get("ok"),
                        "detail": step.get("detail"),
                        "durationSec": step.get("durationSec"),
                    }
                    for step in steps
                ],
                "metrics": {
                    key: metrics.get(key)
                    for key in (
                        "browserMode",
                        "browserVersion",
                        "loginDurationSec",
                        "scheduleRecords",
                        "timePairsInDom",
                        "apiEndpointCount",
                        "taskRecords",
                        "taskLinksInDom",
                        "bodyTextLength",
                        "moodle",
                        "cookieSummary",
                        "referenceHarness",
                        "safeGet",
                        "formAction",
                        "formProbe",
                        "har",
                    )
                    if key in metrics
                },
                "proof": scenario.get("proof"),
                "artifacts": scenario.get("artifacts"),
                "error": scenario.get("error"),
            }
        )
    return {
        "schemaVersion": results.get("schemaVersion"),
        "benchmarkVersion": results.get("benchmarkVersion"),
        "runId": results.get("runId"),
        "date": results.get("date"),
        "startedAt": results.get("startedAt"),
        "finishedAt": results.get("finishedAt"),
        "host": results.get("host"),
        "credentials": results.get("credentials"),
        "totals": results.get("totals"),
        "scenarios": digest,
        "repositoryCopy": True,
    }


def write_results(run: RunDir, results: dict[str, Any], redactor: Redactor) -> Path:
    path = run.results_path()
    write_private_json(path, results, redactor)
    return path


def write_run_summary(run: RunDir, results: dict[str, Any]) -> Path:
    """Sanitized mirror inside the run directory."""
    summary = sanitize_for_repository(results, run)
    path = run.summary_path()
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def write_evidence(run: RunDir, results: dict[str, Any]) -> Path:
    """Sanitized evidence file under ``docs/v4/_evidence/benchmark/<date>/``."""
    directory = EVIDENCE_ROOT / run.date
    directory.mkdir(parents=True, exist_ok=True)
    summary = sanitize_for_repository(results, run)
    path = directory / "live-benchmark-summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readme = directory / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Свидетельства живого бенчмарка\n\n"
            "Санитизированные сводки живых прогонов (`bench/live/`): статусы, шаги, counts, "
            "найденные эндпоинты. Сырые артефакты (HAR, cookie, storage state, скриншоты) "
            "лежат вне репозитория: `~/.local/state/botai-v4-bench/<дата>/<запуск>/`.\n\n"
            "| Файл | Что |\n|---|---|\n"
            "| `live-benchmark-summary.json` | сводка последнего прогона за дату |\n",
            encoding="utf-8",
        )
    return path


def run_header_line(results: dict[str, Any]) -> str:
    totals = results.get("totals", {})
    browser = (results.get("host", {}).get("browserConfig", {}) or {}).get("mode", "?")
    return (
        f"run {results.get('runId')}: {totals.get('success', 0)} success, "
        f"{totals.get('partial', 0)} partial, {totals.get('blocked', 0)} blocked, "
        f"{totals.get('failed', 0)} failed, {totals.get('durationSec', 0)}s; browser={browser}"
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
