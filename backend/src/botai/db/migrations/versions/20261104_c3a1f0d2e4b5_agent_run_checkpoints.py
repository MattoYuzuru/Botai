"""Persisted checkpoints of v4 chat segments: `agent_run_checkpoints` (D-040).

A chat turn that hits a soft limit or a transient provider error continues as another segment of
the same job. The segment resumes from the last masked conversation of its predecessor instead of
re-reading traces, so the conversation has to survive a deferral, a restart of the worker and a
student's next message. Plain JSONB, masked like the traces; rows expire after 72 hours
(`expires_at`) and go away with their thread (CASCADE).

Additive: one new table and its indexes. The downgrade drops exactly that table; checkpoints are
derived, short-lived data (the traces keep the full record), so nothing else is lost.

Revision ID: c3a1f0d2e4b5
Revises: d5b2e7f9a1c3
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c3a1f0d2e4b5"
down_revision = "d5b2e7f9a1c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    op.create_table(
        "agent_run_checkpoints",
        sa.Column("run_id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("thread_id", sa.String(length=26), nullable=False),
        sa.Column("job_id", sa.String(length=26), nullable=True),
        sa.Column("chain_id", sa.String(length=26), nullable=False),
        sa.Column("segment", sa.Integer(), nullable=False),
        sa.Column("parent_run_id", sa.String(length=26), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("resumable", sa.Boolean(), nullable=False),
        sa.Column("messages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("cost_usd_chain", sa.Float(), nullable=False),
        sa.Column("tokens_chain", sa.BigInteger(), nullable=False),
        sa.Column("continuations", sa.Integer(), nullable=False),
        sa.Column("chain_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["chat_threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_agent_run_checkpoints_user_id", "agent_run_checkpoints", ["user_id"], unique=False
    )
    op.create_index(
        "ix_agent_run_checkpoints_thread",
        "agent_run_checkpoints",
        ["user_id", "thread_id", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_run_checkpoints_job",
        "agent_run_checkpoints",
        ["job_id", "segment"],
        unique=False,
    )
    op.create_index(
        "ix_agent_run_checkpoints_expires", "agent_run_checkpoints", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_checkpoints_expires", table_name="agent_run_checkpoints")
    op.drop_index("ix_agent_run_checkpoints_job", table_name="agent_run_checkpoints")
    op.drop_index("ix_agent_run_checkpoints_thread", table_name="agent_run_checkpoints")
    op.drop_index("ix_agent_run_checkpoints_user_id", table_name="agent_run_checkpoints")
    op.drop_table("agent_run_checkpoints")
