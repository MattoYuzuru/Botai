"""Read-only links to a chat thread for other BotAI users: `chat_shares`.

One row per shared thread; the id is the random token in `/shared/<id>`. Additive: one new table.
The downgrade drops exactly that table, which closes every link; threads and messages stay.

Revision ID: e6f3a9b1c2d4
Revises: c3a1f0d2e4b5
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6f3a9b1c2d4"
down_revision = "c3a1f0d2e4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    op.create_table(
        "chat_shares",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("thread_id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["thread_id"], ["chat_threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id"),
    )
    op.create_index(op.f("ix_chat_shares_user_id"), "chat_shares", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_shares_user_id"), table_name="chat_shares")
    op.drop_table("chat_shares")
