"""Sources v4: live sources, playbooks and versions, artifacts, per-source state and priorities.

Additive only: v3 `connections`, `adapter_specs` and `documents` keep working untouched, and
nothing is copied or renamed here. A v4 source belongs to a space and is unique inside it by
`identity_key` (a normalized address, a hashed private link, a file digest or a mailbox) — never by
origin, so two cabinets, tables or calendars of one university are two rows. A playbook is
versioned knowledge (declarative spec and/or code); at most one version of a playbook is active,
the previous active one stays for rollback. Artifacts are content-addressed (`sha256`, bytes under
`storage_ref`); `source_state` keeps the small mutable cursor/site map; `source_priorities` is the
per-kind order of sources for the overlay (the first live source is the primary).

The downgrade drops the new tables only: v3 data was not touched, so it needs no data restore.

Revision ID: e5a1c9b7d342
Revises: d9f3a1c7e582
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a1c9b7d342"
down_revision: str | None = "d9f3a1c7e582"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.func.now()


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    # --- playbooks: shared or owned knowledge, versioned --------------------------------------
    op.create_table(
        "playbooks",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("university_id", sa.String(50), nullable=True),
        sa.Column("owner_id", sa.String(26), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("language", sa.String(20), server_default="declarative", nullable=False),
        sa.Column("current_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "health",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "language IN ('declarative', 'python', 'js')",
            name=op.f("ck_playbooks_language_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_playbooks_owner_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["university_id"],
            ["universities.id"],
            name=op.f("fk_playbooks_university_id_universities"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_playbooks")),
    )
    op.create_index(op.f("ix_playbooks_owner_id"), "playbooks", ["owner_id"], unique=False)
    op.create_index(
        op.f("ix_playbooks_university_id"), "playbooks", ["university_id"], unique=False
    )

    op.create_table(
        "playbook_versions",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("playbook_id", sa.String(26), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column(
            "tests",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("canary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("created_by_run", sa.String(26), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "spec IS NOT NULL OR code IS NOT NULL",
            name=op.f("ck_playbook_versions_payload_present"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_run"],
            ["jobs.id"],
            name=op.f("fk_playbook_versions_created_by_run_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["playbook_id"],
            ["playbooks.id"],
            name=op.f("fk_playbook_versions_playbook_id_playbooks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_playbook_versions")),
        sa.UniqueConstraint(
            "playbook_id",
            "version",
            name=op.f("uq_playbook_versions_playbook_id_version"),
        ),
    )
    # At most one active version per playbook: the one the runner takes. The old one stays.
    op.create_index(
        "uq_playbook_versions_active",
        "playbook_versions",
        ["playbook_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    # --- sources: one row per identity inside a space ------------------------------------------
    op.create_table(
        "sources",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("space_id", sa.String(26), nullable=False),
        sa.Column("university_id", sa.String(50), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("entry_url", sa.Text(), nullable=True),
        sa.Column("identity_key", sa.String(400), nullable=False),
        sa.Column(
            "kinds",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("playbook_id", sa.String(26), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("auth_ref", sa.String(200), nullable=True),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("trust", sa.String(10), server_default="normal", nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("disabled_from", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'probing', 'learning', 'waiting_user', 'active', 'syncing', "
            "'stale', 'needs_reauth', 'needs_link', 'error', 'disabled', 'archived')",
            name=op.f("ck_sources_status_valid"),
        ),
        sa.CheckConstraint(
            "trust IN ('high', 'normal', 'low')",
            name=op.f("ck_sources_trust_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["playbook_id"],
            ["playbooks.id"],
            name=op.f("fk_sources_playbook_id_playbooks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_sources_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["university_id"],
            ["universities.id"],
            name=op.f("fk_sources_university_id_universities"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_sources_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
    )
    op.create_index(op.f("ix_sources_user_id"), "sources", ["user_id"], unique=False)
    op.create_index("ix_sources_space_id_status", "sources", ["space_id", "status"], unique=False)
    op.create_index(op.f("ix_sources_playbook_id"), "sources", ["playbook_id"], unique=False)
    # One live record per normalized identity inside a space; never one per origin.
    op.create_index(
        "uq_sources_space_identity_key",
        "sources",
        ["space_id", "identity_key"],
        unique=True,
        postgresql_where=sa.text("identity_key IS NOT NULL"),
    )

    # --- artifacts, state, priorities ----------------------------------------------------------
    op.create_table(
        "source_artifacts",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("source_id", sa.String(26), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("storage_ref", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("run_id", sa.String(26), nullable=True),
        sa.Column("step", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('page', 'json', 'file', 'har', 'screenshot', 'log', 'doc', 'other')",
            name=op.f("ck_source_artifacts_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["jobs.id"],
            name=op.f("fk_source_artifacts_run_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_artifacts_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_source_artifacts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_artifacts")),
    )
    op.create_index(
        op.f("ix_source_artifacts_user_id"), "source_artifacts", ["user_id"], unique=False
    )
    op.create_index(
        "ix_source_artifacts_source_id_captured_at",
        "source_artifacts",
        ["source_id", "captured_at"],
        unique=False,
    )
    op.create_index("ix_source_artifacts_sha256", "source_artifacts", ["sha256"], unique=False)

    op.create_table(
        "source_state",
        sa.Column("source_id", sa.String(26), nullable=False),
        sa.Column("last_revision", sa.Integer(), nullable=True),
        sa.Column(
            "cursor",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "site_map",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "endpoints",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_state_source_id_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_source_state")),
    )

    op.create_table(
        "source_priorities",
        sa.Column("space_id", sa.String(26), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column(
            "source_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name=op.f("fk_source_priorities_space_id_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("space_id", "kind", name=op.f("pk_source_priorities")),
    )


def downgrade() -> None:
    op.drop_table("source_priorities")
    op.drop_table("source_state")
    op.drop_table("source_artifacts")
    op.drop_table("sources")
    op.drop_table("playbook_versions")
    op.drop_table("playbooks")
