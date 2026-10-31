"""Remove the "My materials" space from the model: drop the unique index of the old bucket.

A space always has a university (`docs/v4/03-sources-and-ux.md` §1.6, FR-060): the owner's decision
deletes "Мои материалы" as a product place. The model no longer creates a space without
`university_id`, the API hides one (`spaces.service.spaces_out`/`list_sources`), and the client
shows only university spaces.

The migration itself is deliberately conservative (D-016 allows recreating production, but data is
not deleted without need):

* existing rows are NOT deleted — a legacy space without `university_id` stays in the table, keeps
  its connections/documents, and is invisible in the API until the owner moves it to a university;
* `university_id` stays nullable for exactly that legacy data: a `NOT NULL` would either fail on
  production data or force a guessed university;
* only the partial unique index `uq_spaces_user_id_materials` is dropped: the model no longer
  promises one "materials" space per user. The downgrade recreates it exactly as it was (the unique
  index of the old revision 20261030_b7d3c91a5e24).

Revision ID: a9c4e2f7b1d6
Revises: f2b7d9c4a6e8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a9c4e2f7b1d6"
down_revision = "f2b7d9c4a6e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_spaces_user_id_materials", table_name="spaces")


def downgrade() -> None:
    op.create_index(
        "uq_spaces_user_id_materials",
        "spaces",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("university_id IS NULL"),
    )
