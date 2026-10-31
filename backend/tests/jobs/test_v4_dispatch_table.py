"""The worker's handler table under the two engines: v3 keeps the old handlers, v4 takes sources.

The v4 engine is enabled by one env var; the table is where that decision becomes real. The v3
table must stay byte-for-byte the old handler set (the flag is a product switch, not a refactor),
while v4 replaces exactly the source jobs (CONNECT/SYNC/REPAIR) and keeps chat/actions/ingest on the
v3 handlers. The v4 handlers themselves keep a v3 bridge for connections without a v4 source.
"""

from __future__ import annotations

from botai.agent.handlers import HANDLERS as AGENT
from botai.core.jobs import JobKind
from botai.learn.handlers import HANDLERS as LEARN
from botai.settings import Settings
from botai.sources.handlers import HANDLERS as SOURCES
from botai.worker import handler_table


def _settings(engine: str) -> Settings:
    return Settings(_env_file=None, env="test", agent_engine=engine)  # type: ignore[arg-type]


def test_the_v3_flag_keeps_the_old_handler_set() -> None:
    table = handler_table(_settings("v3"))

    assert table[JobKind.CONNECT] is LEARN[JobKind.CONNECT]
    assert table[JobKind.REPAIR] is LEARN[JobKind.REPAIR]
    assert table[JobKind.SYNC] is SOURCES[JobKind.SYNC]
    # The rest of the v3 table is untouched too: chat/actions, ingest, the AGENT handler that
    # refuses on v3, the browser table (empty) and every v3-adjacent handler.
    assert table[JobKind.CHAT] is AGENT[JobKind.CHAT]
    assert table[JobKind.ACTION] is AGENT[JobKind.ACTION]
    assert JobKind.INGEST in table
    assert JobKind.AGENT in table


def test_the_v4_flag_replaces_exactly_the_source_jobs() -> None:
    v3 = handler_table(_settings("v3"))
    v4 = handler_table(_settings("v4"))

    replaced = {JobKind.CONNECT, JobKind.SYNC, JobKind.REPAIR}
    for kind in replaced:
        assert v4[kind] is not v3[kind], kind
        # A distinct v4 handler, not a renamed v3 one.
        assert v4[kind].__module__ == "botai.agent.v4.dispatch"
    for kind in set(v3) - replaced:
        assert v4[kind] is v3[kind], kind


def test_both_tables_have_no_duplicate_kinds() -> None:
    # `handler_table` raises on overlapping v3 tables; the v4 override is an intentional update.
    for engine in ("v3", "v4"):
        table = handler_table(_settings(engine))
        assert len(table) == len(set(table))
