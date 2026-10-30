"""Loads the Markdown skills from `agent/prompts/` and builds the stable prefix text.

The prefix is system.md + the skills of the purpose in a fixed order. It contains no dates, names
or per-user data: everything that varies goes after it (agent-harness.md §3), so that DeepSeek's
prefix cache hits between requests.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"
SEPARATOR = "\n\n---\n\n"

# `skill` argument of `harness.run` -> files in this order. An unknown name is a file name.
SKILL_SETS: dict[str, tuple[str, ...]] = {
    "agent": ("agent", "answer", "procedures", "repair"),
    "answer": ("answer", "procedures"),
    "summarize": ("summarize",),
}


@lru_cache
def load_skill(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file() or path.parent != PROMPTS_DIR:
        raise FileNotFoundError(f"no skill {name!r} in {PROMPTS_DIR}")
    return path.read_text("utf-8").strip()


@lru_cache
def build_system_text(skill: str) -> str:
    """system.md followed by the purpose's skills, joined deterministically."""
    names = SKILL_SETS.get(skill, (skill,))
    return SEPARATOR.join([load_skill("system"), *(load_skill(n) for n in names)])
