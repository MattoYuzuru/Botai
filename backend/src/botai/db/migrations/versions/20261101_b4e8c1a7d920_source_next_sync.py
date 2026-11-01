"""Planned background sync of a v4 source: `sources.next_sync_at` (phase 3.3, plan §3.3).

A source is a live record of the agent: after a successful sync the runner plans the next one, and
the worker scheduler replays the saved playbook without a model. The column is nullable, so NULL
keeps meaning "no background planning" (manual/agent sync only).

Backfill: sources that can actually be read in the background (`status` one of
active/stale/error/needs_reauth and an active playbook exists) get the first planned sync within
the next 6–12 hours — `now() + 6h + deterministic jitter(0..6h)` — so enabling the scheduler does
not fire every source at once after deploy. Sources still being set up, disabled, archived or
without a playbook stay NULL: their next sync is a manual one (CONNECT/SYNC by a person or agent),
and the first successful sync then schedules them from `BOTAI_SOURCE_SYNC_INTERVAL_S`.

The interval here is the documented default (21600 s) as a literal: a migration cannot read the
application settings, and the first real sync overwrites this value from the environment.

The downgrade drops exactly the index and the column; v3 tables and other source columns are not
touched, and no data outside this column is lost.

Revision ID: b4e8c1a7d920
Revises: a9c4e2f7b1d6
Create Date: 2026-11-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b4e8c1a7d920"
down_revision = "a9c4e2f7b1d6"
branch_labels = None
depends_on = None

DEFAULT_SYNC_INTERVAL_S = 21_600
PLANNED_STATUSES = ("active", "stale", "error", "needs_reauth")


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    op.add_column(
        "sources", sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_sources_next_sync_at", "sources", ["next_sync_at"], unique=False)
    # Deterministic 0..interval jitter per source (md5 of the ULID): the first tick after deploy
    # spreads the initial syncs instead of starting all of them in the same minute.
    jitter = f"(('x' || substr(md5(id), 1, 8))::bit(32)::bigint % {DEFAULT_SYNC_INTERVAL_S})"
    op.execute(
        sa.text(
            f"""
            UPDATE sources
               SET next_sync_at = now() + make_interval(secs => {DEFAULT_SYNC_INTERVAL_S} + {jitter})
             WHERE playbook_id IS NOT NULL
               AND status IN {PLANNED_STATUSES!r}
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_sources_next_sync_at", table_name="sources")
    op.drop_column("sources", "next_sync_at")
