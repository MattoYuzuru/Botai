"""Switching a source off and on: the status to restore, the buttons the application pins, and the
index that reads one source's classes in a window.

`connections.disabled_from` keeps the status a source had when it was switched off.
`agent_choices` holds the pending buttons (written by the application, bound to actions) before a
setting the agent proposed is changed. `ix_schedule_events_conn_start` serves the read of a schedule
window; it is built CONCURRENTLY (outside the migration's transaction, so writes to the table are not
blocked) and a failed build leaves no invalid index behind.

Revision ID: d9f3a1c7e582
Revises: c8e2f5a9d413
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d9f3a1c7e582"
down_revision: str | None = "c8e2f5a9d413"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_schedule_events_conn_start"


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    op.add_column("connections", sa.Column("disabled_from", sa.String(20), nullable=True))
    op.create_table(
        "agent_choices",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("thread_id", sa.String(26), nullable=False),
        sa.Column("buttons", postgresql.JSONB(), nullable=False),
        sa.Column("armed", postgresql.JSONB(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_agent_choices_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["chat_threads.id"],
            name=op.f("fk_agent_choices_thread_id_chat_threads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_choices")),
        sa.UniqueConstraint("thread_id", name=op.f("uq_agent_choices_thread_id")),
    )
    op.create_index(op.f("ix_agent_choices_user_id"), "agent_choices", ["user_id"])
    # CREATE INDEX CONCURRENTLY cannot run in a transaction. A build that failed halfway leaves an
    # INVALID index of the same name, which IF NOT EXISTS would keep: drop it first.
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
        op.create_index(
            INDEX,
            "schedule_events",
            ["connection_id", "starts_at"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}"))
    op.drop_table("agent_choices")
    op.drop_column("connections", "disabled_from")
