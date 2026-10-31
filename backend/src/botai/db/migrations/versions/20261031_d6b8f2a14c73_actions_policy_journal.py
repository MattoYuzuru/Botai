"""Actions autonomy policy and journal: `action_policies`, `actions_log` (D-006, D-025, D-026).

Additive only: the v3 `action_proposals`/`audit_log` tables keep working behind
`agent_engine=v3`. The v4 path gets the owner's policy per (scope, action class) and one
append-only journal row per concrete proposal — what was shown, decided, sent and returned.
`actions_log.request` stores the method, URL and `body_sha256` only; primary secret values never
reach the table, and `proposal` keeps a redacted preview (docs/v4/02-architecture.md §7).

The downgrade drops both tables; v3 data is untouched.

Revision ID: d6b8f2a14c73
Revises: c3f7a2b9d4e1
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d6b8f2a14c73"
down_revision: str | None = "c3f7a2b9d4e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()

ACTION_CLASSES = ("read", "write_soft", "write_destructive", "money", "auth")
ACTION_MODES = ("auto", "ask", "never")
SCOPE_KINDS = ("global", "site", "space", "source")
DECISIONS = (
    "auto",
    "asked",
    "approved",
    "never",
    "executing",
    "executed",
    "failed",
    "denied",
    "expired",
)


def _in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    op.create_table(
        "action_policies",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("scope_kind", sa.String(8), nullable=False),
        sa.Column("scope_id", sa.String(300), nullable=True),
        sa.Column("action_class", sa.String(20), nullable=False),
        sa.Column("mode", sa.String(6), nullable=False),
        sa.Column(
            "limits",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            f"scope_kind IN ({_in_list(SCOPE_KINDS)})", name="scope_kind_valid"
        ),
        sa.CheckConstraint(
            f"action_class IN ({_in_list(ACTION_CLASSES)})", name="action_class_valid"
        ),
        sa.CheckConstraint(f"mode IN ({_in_list(ACTION_MODES)})", name="mode_valid"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_action_policies_user_id", "action_policies", ["user_id"])
    op.create_index(
        "uq_action_policies_global",
        "action_policies",
        ["user_id", "action_class"],
        unique=True,
        postgresql_where=sa.text("scope_kind = 'global'"),
    )
    op.create_index(
        "uq_action_policies_scoped",
        "action_policies",
        ["user_id", "scope_kind", "scope_id", "action_class"],
        unique=True,
        postgresql_where=sa.text("scope_kind <> 'global'"),
    )

    op.create_table(
        "actions_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("run_id", sa.String(26), nullable=True),
        sa.Column("job_id", sa.String(26), nullable=True),
        sa.Column("source_id", sa.String(26), nullable=True),
        sa.Column(
            "scope_kind", sa.String(8), server_default="global", nullable=False
        ),
        sa.Column("scope_id", sa.String(300), nullable=True),
        sa.Column("action_class", sa.String(20), nullable=False),
        sa.Column("mode", sa.String(6), nullable=False),
        sa.Column("decision", sa.String(20), server_default="asked", nullable=False),
        sa.Column("intent", sa.String(300), server_default="", nullable=False),
        sa.Column(
            "proposal",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("idempotency_key", sa.String(120), nullable=True),
        sa.Column("rollback", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            f"scope_kind IN ({_in_list(SCOPE_KINDS)})", name="scope_kind_valid"
        ),
        sa.CheckConstraint(
            f"action_class IN ({_in_list(ACTION_CLASSES)})", name="action_class_valid"
        ),
        sa.CheckConstraint(f"mode IN ({_in_list(ACTION_MODES)})", name="mode_valid"),
        sa.CheckConstraint(f"decision IN ({_in_list(DECISIONS)})", name="decision_valid"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_actions_log_user_id", "actions_log", ["user_id"])
    op.create_index("ix_actions_log_user_created", "actions_log", ["user_id", "created_at"])
    op.create_index("ix_actions_log_source", "actions_log", ["source_id"])
    op.create_index("ix_actions_log_run", "actions_log", ["run_id"])
    op.create_index("ix_actions_log_decision", "actions_log", ["decision"])
    op.create_index(
        "uq_actions_log_user_idempotency",
        "actions_log",
        ["user_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    for name in (
        "uq_actions_log_user_idempotency",
        "ix_actions_log_decision",
        "ix_actions_log_run",
        "ix_actions_log_source",
        "ix_actions_log_user_created",
        "ix_actions_log_user_id",
    ):
        op.drop_index(name, table_name="actions_log")
    op.drop_table("actions_log")

    for name in (
        "uq_action_policies_scoped",
        "uq_action_policies_global",
        "ix_action_policies_user_id",
    ):
        op.drop_index(name, table_name="action_policies")
    op.drop_table("action_policies")
