"""Where a live run keeps its files: raw session material outside the repository.

Repository (public) rule: only sanitized summaries belong there. Raw HAR, storage state,
screenshots and network logs live under ``~/.local/state/botai-v4-bench/<date>/<run>/`` with
mode 0700; files that contain session material are written with mode 0600.

Layout of one run::

    <state root>/2026-10-31/run-20261031-134500/
    ├── results.json                 # full report of the run (no primary secrets)
    ├── raw/
    │   ├── hse-login/  ...
    │   ├── hse-session/ ...
    │   └── cu-tasks/   ...
    └── sanitized/
        └── summary.json             # copy that may be published under docs/v4/_evidence
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = REPO_ROOT / "docs" / "v4" / "_evidence" / "benchmark"
STATE_ROOT = Path(os.environ.get("BOTAI_BENCH_STATE", "~/.local/state/botai-v4-bench")).expanduser()


def ensure_private_dir(path: Path) -> Path:
    """Create a directory tree readable only by the owner."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@dataclass(slots=True)
class RunDir:
    root: Path
    date: str
    run_id: str

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def sanitized(self) -> Path:
        return self.root / "sanitized"

    def scenario_dir(self, name: str) -> Path:
        path = self.raw / name
        ensure_private_dir(path)
        return path

    def results_path(self) -> Path:
        return self.root / "results.json"

    def summary_path(self) -> Path:
        return self.sanitized / "summary.json"

    def relative(self, path: Path) -> str:
        """Path relative to the run root, for reports (never an absolute home path)."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)


def make_run_dir(*, now: datetime | None = None, date: str | None = None) -> RunDir:
    moment = now or datetime.now().astimezone()
    date_str = date or moment.strftime("%Y-%m-%d")
    base = ensure_private_dir(STATE_ROOT / date_str)
    for sequence in range(1, 100):
        run_id = f"run-{moment.strftime('%Y%m%d-%H%M%S')}"
        if sequence > 1:
            run_id = f"{run_id}-{sequence}"
        root = base / run_id
        try:
            root.mkdir(parents=True)
            root.chmod(0o700)
        except FileExistsError:
            continue
        run = RunDir(root=root, date=date_str, run_id=run_id)
        ensure_private_dir(run.raw)
        ensure_private_dir(run.sanitized)
        return run
    raise RuntimeError("cannot allocate a unique run directory")


def evidence_dir(date: str) -> Path:
    """Repository directory for sanitized evidence of a given date."""
    path = EVIDENCE_ROOT / date
    path.mkdir(parents=True, exist_ok=True)
    return path


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class Timer:
    """Monotonic duration helper for scenario steps."""

    started: float = field(default_factory=lambda: __import__("time").monotonic())

    def elapsed(self) -> float:
        return round(__import__("time").monotonic() - self.started, 3)

    def reset(self) -> None:
        self.started = __import__("time").monotonic()
