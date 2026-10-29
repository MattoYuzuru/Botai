"""Add private encrypted research checkpoints without changing existing rows.

Revision ID: e4a91b7c603d
Revises: 9c2a7184be6d
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a91b7c603d"
down_revision: str | None = "9c2a7184be6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))
    op.create_table(
        "job_research_checkpoints",
        sa.Column("job_id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("connection_id", sa.String(26), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("intent_key", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kek_id", sa.String(20), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("aad_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "revision > 0", name=op.f("ck_job_research_checkpoints_revision_positive")
        ),
        sa.CheckConstraint(
            "format_version > 0", name=op.f("ck_job_research_checkpoints_format_version_positive")
        ),
        sa.CheckConstraint(
            "intent_key ~ '^[0-9a-f]{64}$'", name=op.f("ck_job_research_checkpoints_intent_key_hex")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_job_research_checkpoints_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_job_research_checkpoints_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_job_research_checkpoints_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_job_research_checkpoints")),
    )
    op.create_index("ix_job_research_checkpoints_user_id", "job_research_checkpoints", ["user_id"])
    op.create_index(
        "ix_job_research_checkpoints_connection_id", "job_research_checkpoints", ["connection_id"]
    )
    op.create_index(
        "ix_job_research_checkpoints_expires_at", "job_research_checkpoints", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("job_research_checkpoints")
