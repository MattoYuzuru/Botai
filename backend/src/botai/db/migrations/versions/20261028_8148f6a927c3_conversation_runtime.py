"""Add durable connection conversation context and request identities.

Revision ID: 8148f6a927c3
Revises: 2abd6565e662
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8148f6a927c3"
down_revision: str | None = "2abd6565e662"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))
    op.add_column(
        "chat_threads", sa.Column("purpose", sa.String(10), server_default="chat", nullable=False)
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
    op.add_column("chat_threads", sa.Column("client_request_id", sa.String(26), nullable=True))
    op.create_unique_constraint(
        "uq_chat_threads_user_id_client_request_id",
        "chat_threads",
        ["user_id", "client_request_id"],
    )
    op.add_column("chat_messages", sa.Column("client_message_id", sa.String(26), nullable=True))
    op.create_unique_constraint(
        "uq_chat_messages_thread_id_client_message_id",
        "chat_messages",
        ["thread_id", "client_message_id"],
    )
    op.create_index("ix_chat_messages_user_job", "chat_messages", ["user_id", "job_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_user_job", table_name="chat_messages")
    op.drop_constraint(
        "uq_chat_messages_thread_id_client_message_id", "chat_messages", type_="unique"
    )
    op.drop_column("chat_messages", "client_message_id")
    op.drop_constraint("uq_chat_threads_user_id_client_request_id", "chat_threads", type_="unique")
    op.drop_column("chat_threads", "client_request_id")
    op.drop_column("chat_threads", "connect_context")
    op.drop_column("chat_threads", "purpose")
