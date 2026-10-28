"""Persist connection-specific read scopes without changing shared recipes.

Revision ID: 9c2a7184be6d
Revises: 8148f6a927c3
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9c2a7184be6d"
down_revision: str | None = "8148f6a927c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))
    op.add_column("connections", sa.Column("data_scope", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("connections", "data_scope")
