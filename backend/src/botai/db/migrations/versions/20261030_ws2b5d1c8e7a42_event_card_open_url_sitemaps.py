"""Event card fields, the page that shows a record (open_url) and shared site maps.

Revision ID: b5d1c8e7a42f
Revises: b7d3c91a5e24
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5d1c8e7a42f"
down_revision: str | None = "b7d3c91a5e24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_TABLES = (
    "courses",
    "schedule_events",
    "assignments",
    "deadlines",
    "grades",
    "people",
    "locations",
    "news_items",
)


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))
    for table in DOMAIN_TABLES:
        op.add_column(table, sa.Column("open_url", sa.Text(), nullable=True))
    op.add_column("schedule_events", sa.Column("kind_text", sa.String(120), nullable=True))
    op.add_column("schedule_events", sa.Column("address", sa.String(300), nullable=True))
    op.add_column("schedule_events", sa.Column("group_text", sa.String(200), nullable=True))
    op.add_column("schedule_events", sa.Column("lesson_number", sa.Integer(), nullable=True))
    for column in ("links", "attributes"):
        op.add_column(
            "schedule_events",
            sa.Column(
                column,
                postgresql.JSONB(),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
        )
    op.create_table(
        "source_sitemaps",
        sa.Column("origin", sa.String(255), nullable=False),
        sa.Column("map", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("origin", name=op.f("pk_source_sitemaps")),
    )


def downgrade() -> None:
    op.drop_table("source_sitemaps")
    for column in ("attributes", "links", "lesson_number", "group_text", "address", "kind_text"):
        op.drop_column("schedule_events", column)
    for table in reversed(DOMAIN_TABLES):
        op.drop_column(table, "open_url")
