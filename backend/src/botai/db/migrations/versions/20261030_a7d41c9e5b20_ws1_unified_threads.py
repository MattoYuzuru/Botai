"""One conversation kind: threads lose purpose/connect_context and gain read state; agent memory.

Revision ID: a7d41c9e5b20
Revises: e4a91b7c603d
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7d41c9e5b20"
down_revision: str | None = "e4a91b7c603d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    op.add_column(
        "chat_threads",
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "chat_threads", sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE chat_threads t SET last_message_at = COALESCE("
            "(SELECT max(m.created_at) FROM chat_messages m WHERE m.thread_id = t.id), "
            "t.created_at)"
        )
    )
    # Everything written before this release has already been seen by its reader.
    op.execute(sa.text("UPDATE chat_threads SET last_read_at = last_message_at"))
    op.drop_column("chat_threads", "purpose")
    op.drop_column("chat_threads", "connect_context")
    op.drop_column("chat_threads", "updated_at")
    op.create_index("ix_jobs_thread", "jobs", ["thread_id"])
    op.create_table(
        "user_memory",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("note", sa.String(300), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_memory_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_memory")),
    )
    op.create_index(op.f("ix_user_memory_user_id"), "user_memory", ["user_id"])
    op.create_index("ix_user_memory_user_created", "user_memory", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_user_memory_user_created", table_name="user_memory")
    op.drop_index(op.f("ix_user_memory_user_id"), table_name="user_memory")
    op.drop_table("user_memory")
    op.drop_index("ix_jobs_thread", table_name="jobs")
    op.add_column(
        "chat_threads",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.add_column(
        "chat_threads",
        sa.Column(
            "connect_context",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "chat_threads", sa.Column("purpose", sa.String(10), server_default="chat", nullable=False)
    )
    op.drop_column("chat_threads", "last_read_at")
    op.drop_column("chat_threads", "last_message_at")
