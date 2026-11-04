"""Panels of «Учёба» and two new data kinds: `panels`, `profile_fields`, `ranking_entries`.

D-039 (docs/v4/06-decisions.md, docs/v4/23-panels-backend.md). Additive only:

* `panels` — thin view configs over the domain data of one university (type, validated `spec`,
  student's switches, life of an agent proposal). `space_id` and `user_id` cascade, the source and
  the proposing job are `SET NULL`; the key is unique inside the space (idempotent proposals).
* `profile_fields` and `ranking_entries` — domain tables of the v4-only kinds `profile` and
  `ranking`, with the same provenance columns as the other domain tables (`source_id`, partial
  unique `(source_id, external_id)`); `connection_id` exists for the shared writer and stays NULL.
* `grades.value_num` and `grades.scale_max` — the number and the top of the scale when the source
  gave both unambiguously. Existing rows stay NULL until their source syncs again; nothing is
  computed here.

The downgrade drops exactly what the upgrade adds. Rows of the new tables go with them (they hold
no data that another table lacks: a source sync recreates them).

Revision ID: d5b2e7f9a1c3
Revises: b4e8c1a7d920
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5b2e7f9a1c3"
down_revision: str | None = "b4e8c1a7d920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()


def _provenance_columns() -> list[sa.Column[object]]:
    """The columns every domain table shares (`db.models._Domain`)."""
    return [
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("connection_id", sa.String(26), nullable=True),
        sa.Column("source_id", sa.String(26), nullable=True),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("open_url", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spec_revision", sa.Integer(), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _provenance_constraints(table: str) -> list[sa.schema.SchemaItem]:
    return [
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f(f"fk_{table}_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f(f"fk_{table}_source_id_sources"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f(f"fk_{table}_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint("connection_id", "external_id", name=f"uq_{table}_conn_ext"),
    ]


def _provenance_indexes(table: str) -> None:
    op.create_index(op.f(f"ix_{table}_connection_id"), table, ["connection_id"], unique=False)
    op.create_index(op.f(f"ix_{table}_source_id"), table, ["source_id"], unique=False)
    op.create_index(op.f(f"ix_{table}_user_id"), table, ["user_id"], unique=False)
    op.create_index(
        f"uq_{table}_source_ext",
        table,
        ["source_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    # --- grades: the number and the scale, only when the source gave both ------------------
    op.add_column("grades", sa.Column("value_num", sa.Float(), nullable=True))
    op.add_column("grades", sa.Column("scale_max", sa.Float(), nullable=True))

    # --- profile: the student's card, one row per field ------------------------------------
    op.create_table(
        "profile_fields",
        sa.Column("field", sa.String(100), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("value", sa.String(500), nullable=False),
        sa.Column("group_text", sa.String(100), nullable=True),
        *_provenance_columns(),
        *_provenance_constraints("profile_fields"),
    )
    _provenance_indexes("profile_fields")

    # --- ranking: one row per place of one board -------------------------------------------
    op.create_table(
        "ranking_entries",
        sa.Column("board", sa.String(120), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("score", sa.String(60), nullable=True),
        sa.Column("score_num", sa.Float(), nullable=True),
        sa.Column("is_me", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("period", sa.String(60), nullable=True),
        *_provenance_columns(),
        *_provenance_constraints("ranking_entries"),
    )
    _provenance_indexes("ranking_entries")
    op.create_index(
        "ix_ranking_entries_user_board", "ranking_entries", ["user_id", "board"], unique=False
    )

    # --- panels: view configs, no data ------------------------------------------------------
    op.create_table(
        "panels",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("space_id", sa.String(26), nullable=False),
        sa.Column("source_id", sa.String(26), nullable=True),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(10), server_default="active", nullable=False),
        sa.Column("visible", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("hidden_in_all", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("proposed_by_job_id", sa.String(26), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("origin IN ('system', 'agent')", name=op.f("ck_panels_origin_valid")),
        sa.CheckConstraint(
            "status IN ('proposed', 'active', 'dismissed')", name=op.f("ck_panels_status_valid")
        ),
        sa.CheckConstraint(
            "type IN ('kpi', 'chart', 'table', 'ranking', 'profile', 'list')",
            name=op.f("ck_panels_type_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["proposed_by_job_id"],
            ["jobs.id"],
            name=op.f("fk_panels_proposed_by_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_panels_source_id_sources"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["spaces.id"], name=op.f("fk_panels_space_id_spaces"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_panels_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_panels")),
        sa.UniqueConstraint("space_id", "key", name="uq_panels_space_id_key"),
    )
    op.create_index("ix_panels_space_id_position", "panels", ["space_id", "position"], unique=False)
    op.create_index(op.f("ix_panels_user_id"), "panels", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_panels_user_id"), table_name="panels")
    op.drop_index("ix_panels_space_id_position", table_name="panels")
    op.drop_table("panels")
    op.drop_table("ranking_entries")  # its indexes go with the table
    op.drop_table("profile_fields")
    op.drop_column("grades", "scale_max")
    op.drop_column("grades", "value_num")
