"""Domain provenance of v4 sources: `source_id` next to the v3 `connection_id`.

A v4 source is a live record of its own (`sources`) and has no `connections` row, so the shared
domain tables get a second, nullable provenance column: `source_id` references `sources` with
`ON DELETE SET NULL` (deleting a source keeps the data it brought). `connection_id` becomes
nullable for the same reason; old v3 rows keep their value and are not rewritten. Natural keys are
enforced per path: the existing `UNIQUE(connection_id, external_id)` stays for v3 rows, and a
partial unique index `(source_id, external_id) WHERE source_id IS NOT NULL` gives v4 rows the same
idempotency (a plain UNIQUE over a nullable column would not constrain NULL rows).

This is the bridge step of docs/v4/03-sources-and-ux §6.1 ("source_id добавляется и заполняется до
удаления connection_id"): nothing is renamed or dropped, reads that still go through connections
keep working, and a v4 row is visible by its own provenance.

The downgrade deletes rows that only a v4 source could have written (`connection_id IS NULL`) —
they cannot exist in a schema without `source_id` — and restores `NOT NULL`; v3 rows are untouched.

Revision ID: c3f7a2b9d4e1
Revises: a8d4f1c2b6e9
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f7a2b9d4e1"
down_revision: str | None = "a8d4f1c2b6e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table a v4 reading kind maps to (runner.V4_TO_KIND); each has `external_id` already.
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
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    for table in DOMAIN_TABLES:
        op.add_column(table, sa.Column("source_id", sa.String(26), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_source_id_sources",
            table,
            "sources",
            ["source_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(f"ix_{table}_source_id", table, ["source_id"], unique=False)
        op.create_index(
            f"uq_{table}_source_ext",
            table,
            ["source_id", "external_id"],
            unique=True,
            postgresql_where=sa.text("source_id IS NOT NULL"),
        )
        op.alter_column(table, "connection_id", existing_type=sa.String(26), nullable=True)


def downgrade() -> None:
    for table in DOMAIN_TABLES:
        # A v4 row has no connection to fall back to: it cannot outlive this revision. Rows of the
        # v3 path (`connection_id` set) are not touched.
        op.execute(sa.text(f"DELETE FROM {table} WHERE connection_id IS NULL"))  # noqa: S608
        op.alter_column(table, "connection_id", existing_type=sa.String(26), nullable=False)
        op.drop_index(f"uq_{table}_source_ext", table_name=table)
        op.drop_index(f"ix_{table}_source_id", table_name=table)
        op.drop_constraint(f"fk_{table}_source_id_sources", table, type_="foreignkey")
        op.drop_column(table, "source_id")
