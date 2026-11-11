"""Grades as a system, the source's status words and the student's corrections (D-043…D-045).

docs/v4/27-study-hub-grades-fastpath.md §3–§5. Additive only, safe on a live database:

* `grades`: `layer`, `period`, `assignment_external_id`, `url`, `comment`, `changed_at` — all
  nullable, no default, so PostgreSQL adds them without rewriting the table. Existing rows stay
  NULL until their source syncs again; nothing is computed here.
* `deadlines`: `state_text`, `user_state`, `changed_at` — nullable, same reasoning.
* `user_memory.kind` — `note` | `rule`, NOT NULL with a constant server default (a metadata-only
  change since PostgreSQL 11); every existing memory row becomes a `note`.
* `status_rules` — new table: one rule per (user, source, normalized label); NULL `source_id`
  means every source of the user, so uniqueness is two partial unique indexes.

The downgrade drops exactly what the upgrade adds. The student's rules and marks go with it (the
source's own data stays; a sync recomputes `state`).

Revision ID: f7c2d8e4a5b6
Revises: e6f3a9b1c2d4
Create Date: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7c2d8e4a5b6"
down_revision = "e6f3a9b1c2d4"
branch_labels = None
depends_on = None

GRADE_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("layer", sa.String(length=12)),
    ("period", sa.String(length=60)),
    ("assignment_external_id", sa.String(length=200)),
    ("url", sa.Text()),
    ("comment", sa.Text()),
    ("changed_at", sa.DateTime(timezone=True)),
)
DEADLINE_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("state_text", sa.String(length=120)),
    ("user_state", sa.String(length=12)),
    ("changed_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    for name, type_ in GRADE_COLUMNS:
        op.add_column("grades", sa.Column(name, type_, nullable=True))
    for name, type_ in DEADLINE_COLUMNS:
        op.add_column("deadlines", sa.Column(name, type_, nullable=True))

    op.add_column(
        "user_memory",
        sa.Column("kind", sa.String(length=8), server_default="note", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_user_memory_kind_valid"), "user_memory", "kind IN ('note', 'rule')"
    )

    op.create_table(
        "status_rules",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("source_id", sa.String(length=26), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("state", sa.String(length=12), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('open', 'submitted', 'graded', 'missed')",
            name=op.f("ck_status_rules_state_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_status_rules_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_status_rules_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_status_rules")),
    )
    op.create_index(op.f("ix_status_rules_user_id"), "status_rules", ["user_id"], unique=False)
    op.create_index(op.f("ix_status_rules_source_id"), "status_rules", ["source_id"], unique=False)
    op.create_index(
        "uq_status_rules_user_source_label",
        "status_rules",
        ["user_id", "source_id", "label"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )
    op.create_index(
        "uq_status_rules_user_label_all",
        "status_rules",
        ["user_id", "label"],
        unique=True,
        postgresql_where=sa.text("source_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("status_rules")  # its indexes go with the table
    op.drop_constraint(op.f("ck_user_memory_kind_valid"), "user_memory", type_="check")
    op.drop_column("user_memory", "kind")
    for name, _ in reversed(DEADLINE_COLUMNS):
        op.drop_column("deadlines", name)
    for name, _ in reversed(GRADE_COLUMNS):
        op.drop_column("grades", name)
