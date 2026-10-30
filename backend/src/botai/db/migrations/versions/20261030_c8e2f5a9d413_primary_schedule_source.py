"""The source that decides when several sources of one university show the same class.

`spaces.primary_schedule_connection_id` points at one of the university's own sources. NULL keeps
the default (the oldest live source with schedule events). Deleting the source clears the pointer;
disabling it leaves the pointer and the read passes over it.

Revision ID: c8e2f5a9d413
Revises: a7d41c9e5b20
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2f5a9d413"
down_revision: str | None = "a7d41c9e5b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK = "fk_spaces_primary_schedule_connection_id_connections"


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))
    op.add_column(
        "spaces", sa.Column("primary_schedule_connection_id", sa.String(26), nullable=True)
    )
    op.create_foreign_key(
        FK,
        "spaces",
        "connections",
        ["primary_schedule_connection_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(FK, "spaces", type_="foreignkey")
    op.drop_column("spaces", "primary_schedule_connection_id")
