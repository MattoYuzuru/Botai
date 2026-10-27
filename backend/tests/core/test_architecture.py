"""Guards from AGENTS.md that are cheap to check mechanically."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "botai"

# FR-032: nothing for a concrete university in code. Extend the list when a new pilot appears.
REAL_UNIVERSITIES = re.compile(
    r"hse\.ru|centraluniversity|misis|mirea|msu\.ru|spbu|itmo|urfu|modeus|edu\.hse|lk\.hse",
    re.IGNORECASE,
)


def _py_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "_generated" not in p.parts]


def test_no_university_specific_code() -> None:
    offenders = [
        str(p.relative_to(SRC))
        for p in _py_files()
        if REAL_UNIVERSITIES.search(p.read_text("utf-8"))
    ]
    prompts = (
        [p for p in (SRC / "agent" / "prompts").rglob("*.md")]
        if (SRC / "agent" / "prompts").exists()
        else []
    )
    offenders += [
        str(p.relative_to(SRC)) for p in prompts if REAL_UNIVERSITIES.search(p.read_text("utf-8"))
    ]
    assert offenders == [], f"university-specific code found: {offenders}"


def test_no_direct_http_outside_core() -> None:
    """Outbound requests must use botai.core.http (timeouts, retries, egress allowlist)."""
    allowed = {"core/http.py", "agent/llm.py"}
    pattern = re.compile(r"httpx\.(AsyncClient|Client|get|post|request)\(")
    offenders = [
        str(p.relative_to(SRC))
        for p in _py_files()
        if str(p.relative_to(SRC)) not in allowed and pattern.search(p.read_text("utf-8"))
    ]
    assert offenders == [], offenders
