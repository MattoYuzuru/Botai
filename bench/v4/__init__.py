"""v4 agent acceptance bench (lane E, independent verification): real wiring, honest evidence.

This package is **not** product code. It builds the same `botai.agent.v4.factory.build_agent_wiring`
that the `AGENT` job handler builds, drives it with a real provider and real Chromium, and writes a
sanitized summary for the repository. Two differences from production are deliberate:

* the owner's answers come from a test-only ask channel (macOS Keychain through
  `bench.live.secret_broker`) instead of the PWA; the model still sees only secret handles;
* the trace store is in-memory, so the bench needs no Postgres and never writes the dev DB.

Entry point (from the repository root, with the backend virtualenv):

    backend/.venv/bin/python -m bench.v4.run_agent --scenario live-hse

Raw artifacts (traces, messages, the agent's workspace) stay outside the repository under
``~/.local/state/botai-v4-bench/`` (0700, files 0600); only a sanitized summary goes to
``docs/v4/_evidence/``. See ``docs/v4/04-benchmark.md`` and the evidence README.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
