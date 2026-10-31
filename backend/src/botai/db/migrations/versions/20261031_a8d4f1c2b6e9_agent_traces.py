"""Agent v4 traces: one row per run and one row per step (`agent_runs`, `agent_run_events`).

Additive only: the v3 queue and job events stay untouched. A run row carries the counters and the
status the runtime returns; steps are the same `TraceEvent`s `MemoryTraceStore` keeps in memory,
so `trace_read`, the API and the UI see one trace after a restart (D-014). `job_id` is nullable
with `ON DELETE SET NULL`: the queue purges finished jobs, the trace should outlive them.

The payloads are bounded JSON data and must already be secret-free when written: tool handlers
mask primary secrets before a result reaches the runtime (docs/v4/15-runtime-wiring.md §4).

Revision ID: a8d4f1c2b6e9
Revises: b7e2c4a91d3f
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8d4f1c2b6e9"
down_revision: str | None = "b7e2c4a91d3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("job_id", sa.String(26), nullable=True),
        sa.Column("task", sa.Text(), server_default="", nullable=False),
        sa.Column("provider", sa.String(40), server_default="", nullable=False),
        sa.Column("model", sa.String(120), server_default="", nullable=False),
        sa.Column("status", sa.String(20), server_default="running", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("steps", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Float(), server_default="0", nullable=False),
        sa.Column("event_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_user_started", "agent_runs", ["user_id", "started_at"])
    op.create_index("ix_agent_runs_job", "agent_runs", ["job_id"])

    op.create_table(
        "agent_run_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(26), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("step", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_agent_run_events_run_seq"),
    )
    op.create_index("ix_agent_run_events_run_seq", "agent_run_events", ["run_id", "seq"])


def downgrade() -> None:
    for name in ("ix_agent_run_events_run_seq",):
        op.drop_index(name, table_name="agent_run_events")
    op.drop_table("agent_run_events")
    for name in (
        "ix_agent_runs_job",
        "ix_agent_runs_user_started",
        "ix_agent_runs_user_id",
    ):
        op.drop_index(name, table_name="agent_runs")
    op.drop_table("agent_runs")
