"""RAG: embeddings of chunks, portable now and pgvector-ready later (docs/v4/18-rag.md).

Additive only: one new table next to `chunks`/`documents`; v3 FTS rows are untouched. The vector is
stored as JSONB because dev, CI and production all run plain `postgres:17-alpine`, where the
`vector` extension is unavailable; the migration therefore needs no extension and cannot break a
rollout. `pgvector` comes as a separate, explicit step when the images change: add
`embedding_vec vector(384)` and an HNSW index, keep JSONB as the portable source of truth.

`chunk_embeddings` keeps provenance (source, artifact, url, title, kind, capture time, trust) next to
the vector, so hybrid search can return «где прочитано и насколько свежо» without extra joins for the
artifact path. The unique key `(chunk_id, model)` lets several embedding models coexist.

Revision ID: b7e2c4a91d3f
Revises: e5a1c9b7d342
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7e2c4a91d3f"
down_revision: str | None = "e5a1c9b7d342"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.String(26), nullable=False),
        sa.Column("user_id", sa.String(26), nullable=False),
        sa.Column("chunk_id", sa.String(26), nullable=False),
        sa.Column("document_id", sa.String(26), nullable=False),
        sa.Column("source_id", sa.String(26), nullable=True),
        sa.Column("artifact_id", sa.String(26), nullable=True),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("kind", sa.String(20), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trust", sa.String(10), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["artifact_id"], ["source_artifacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chunk_id", "model", name="uq_chunk_embeddings_chunk_model"),
    )
    op.create_index("ix_chunk_embeddings_user_id", "chunk_embeddings", ["user_id"])
    op.create_index("ix_chunk_embeddings_chunk_id", "chunk_embeddings", ["chunk_id"])
    op.create_index("ix_chunk_embeddings_document_id", "chunk_embeddings", ["document_id"])
    op.create_index("ix_chunk_embeddings_user_id_model", "chunk_embeddings", ["user_id", "model"])
    op.create_index("ix_chunk_embeddings_source_id", "chunk_embeddings", ["source_id"])
    op.create_index("ix_chunk_embeddings_content_hash", "chunk_embeddings", ["content_hash"])


def downgrade() -> None:
    # Only the new table is dropped; pgvector (if it was ever added by a later migration) and all
    # v3/v4 data stay untouched.
    for name in (
        "ix_chunk_embeddings_content_hash",
        "ix_chunk_embeddings_source_id",
        "ix_chunk_embeddings_user_id_model",
        "ix_chunk_embeddings_document_id",
        "ix_chunk_embeddings_chunk_id",
        "ix_chunk_embeddings_user_id",
    ):
        op.drop_index(name, table_name="chunk_embeddings")
    op.drop_table("chunk_embeddings")
