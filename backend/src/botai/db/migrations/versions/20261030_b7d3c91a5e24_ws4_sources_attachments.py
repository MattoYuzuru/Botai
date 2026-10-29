"""One space per university, human source titles and kinds, chat attachments.

* `spaces`: duplicates of one (user, university) pair, and extra "no university" spaces, are merged
  into the oldest one (connections, documents, facts and threads move; its profile is kept),
  then two partial unique indexes enforce the rule. A space of a university takes its name.
* `connections`: `entry_url`; kinds move to the new vocabulary (`mail` -> `other`, a document
  behind a link -> `doc`); a title that is just the host of the origin becomes the kind's name.
* `attachments`: the bytes of chat files, owned by one user.

Revision ID: b7d3c91a5e24
Revises: e4a91b7c603d
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d3c91a5e24"
down_revision: str | None = "e4a91b7c603d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPLOAD_ORIGIN = "https://upload.botai.local"
KIND_TITLES = {
    "portal": "Личный кабинет",
    "lms": "Система обучения",
    "schedule": "Расписание",
    "site": "Сайт",
    "sheet": "Таблица",
    "doc": "Документ",
    "document": "Документ",
    "calendar": "Календарь",
    "other": "Источник",
}


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    # --- spaces: merge duplicates into the oldest, then forbid them ----------------------------
    op.execute(
        sa.text(
            """
            CREATE TEMP TABLE _space_merge ON COMMIT DROP AS
            SELECT id AS old_id, keeper AS new_id FROM (
                SELECT id, first_value(id) OVER (
                    PARTITION BY user_id, university_id ORDER BY created_at, id
                ) AS keeper FROM spaces
            ) ranked WHERE id <> keeper
            """
        )
    )
    for table in ("connections", "documents", "facts", "chat_threads"):
        op.execute(
            sa.text(
                f"UPDATE {table} t SET space_id = m.new_id "
                "FROM _space_merge m WHERE t.space_id = m.old_id"
            )
        )
    op.execute(sa.text("DELETE FROM spaces s USING _space_merge m WHERE s.id = m.old_id"))
    op.execute(
        sa.text(
            "UPDATE spaces s SET title = left(u.title, 120) "
            "FROM universities u WHERE s.university_id = u.id"
        )
    )
    op.create_index(
        "uq_spaces_user_id_university_id",
        "spaces",
        ["user_id", "university_id"],
        unique=True,
        postgresql_where=sa.text("university_id IS NOT NULL"),
    )
    op.create_index(
        "uq_spaces_user_id_materials",
        "spaces",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("university_id IS NULL"),
    )

    # --- connections: entry_url, kinds, human titles -------------------------------------------
    op.add_column("connections", sa.Column("entry_url", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE connections SET kind = 'other' WHERE kind = 'mail'"))
    op.execute(
        sa.text(
            "UPDATE connections SET kind = 'doc' "
            "WHERE kind = 'document' AND origin <> :upload"
        ).bindparams(upload=UPLOAD_ORIGIN)
    )
    op.execute(
        sa.text(
            "UPDATE connections SET kind = 'other' "
            "WHERE kind NOT IN ('portal','lms','schedule','site','sheet','doc','document',"
            "'calendar','other')"
        )
    )
    bare = r"'^(https?://)?(www\.)?|/+$'"  # scheme, "www." and trailing slashes do not make a name
    for kind, title in KIND_TITLES.items():
        op.execute(
            sa.text(
                "UPDATE connections SET title = :title WHERE kind = :kind AND "
                f"regexp_replace(lower(title), {bare}, '', 'g') = "
                f"regexp_replace(lower(origin), {bare}, '', 'g')"
            ).bindparams(title=title, kind=kind)
        )

    # --- attachments ---------------------------------------------------------------------------
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("thread_id", sa.String(26), nullable=True),
        sa.Column("message_id", sa.String(26), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("mime", sa.String(120), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(26), nullable=True),
        sa.Column("ingest_state", sa.String(10), server_default="none", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_attachments_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["chat_messages.id"],
            name=op.f("fk_attachments_message_id_chat_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["chat_threads.id"],
            name=op.f("fk_attachments_thread_id_chat_threads"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_attachments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachments")),
    )
    op.create_index(op.f("ix_attachments_user_id"), "attachments", ["user_id"], unique=False)
    op.create_index(
        "ix_attachments_user_id_thread_id", "attachments", ["user_id", "thread_id"], unique=False
    )


def downgrade() -> None:
    op.drop_table("attachments")
    op.drop_column("connections", "entry_url")
    op.drop_index("uq_spaces_user_id_materials", table_name="spaces")
    op.drop_index("uq_spaces_user_id_university_id", table_name="spaces")
