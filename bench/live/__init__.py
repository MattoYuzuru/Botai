"""Live benchmark of the BotAI v4 free harness (L3): secret credentials, real login, artifacts.

The owner's credentials are never visible to the agent: they live in the macOS Keychain and are
served only to the code that types them into a browser field (``secret_broker.py``). The benchmark
records what really happened -- HAR, cookies, screenshots, console, network, a sanitized
``results.json`` -- so both the agent and the owner can check what worked and where it stopped.

Entry point (from the repository root):

    python -m bench.live.run --scenario all

Rationale, scenario matrix and measured results: ``docs/v4/04-benchmark.md``.
Raw artifacts stay outside the public repository, under ``~/.local/state/botai-v4-bench/``.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
