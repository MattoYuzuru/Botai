"""Switching a source off and on: the status to restore, the questions the application asks first,
and the index that reads one source's classes in a window.

`connections.disabled_from` keeps the status a source had when it was switched off.
`agent_confirmations` holds the pending question (buttons written by the application) before an
action the agent proposed. `ix_schedule_events_conn_start` serves the read of a schedule window.

Revision ID: d9f3a1c7e582
Revises: c8e2f5a9d413
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9f3a1c7e582"
down_revision: str | None = "c8e2f5a9d413"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    op.add_column("connections", sa.Column("disabled_from", sa.String(20), nullable=True))
    op.create_index(
        "ix_schedule_events_conn_start", "schedule_events", ["connection_id", "starts_at"]
    )
    op.create_table(
        "agent_confirmations",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("thread_id", sa.String(26), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("connection_id", sa.String(26), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_agent_confirmations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["chat_threads.id"],
            name=op.f("fk_agent_confirmations_thread_id_chat_threads"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_agent_confirmations_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_confirmations")),
        sa.UniqueConstraint(
            "thread_id", "action", name=op.f("uq_agent_confirmations_thread_id_action")
        ),
    )
    op.create_index(
        op.f("ix_agent_confirmations_user_id"), "agent_confirmations", ["user_id"]
    )
    op.create_index(
        op.f("ix_agent_confirmations_thread_id"), "agent_confirmations", ["thread_id"]
    )
    op.create_index(
        op.f("ix_agent_confirmations_connection_id"), "agent_confirmations", ["connection_id"]
    )


def downgrade() -> None:
    op.drop_table("agent_confirmations")
    op.drop_index("ix_schedule_events_conn_start", table_name="schedule_events")
    op.drop_column("connections", "disabled_from")
