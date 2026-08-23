"""Drop legacy 384-dim embedding column and its index.

Run ONLY after verifying Phase 4 search quality (eval harness passes).
Phase 5 of gemini-embedding-001 migration — cleanup.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP INDEX IF EXISTS facts_embedding_hnsw")
    op.execute("ALTER TABLE facts DROP COLUMN IF EXISTS embedding")


def downgrade():
    op.execute("ALTER TABLE facts ADD COLUMN IF NOT EXISTS embedding vector(384)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS facts_embedding_hnsw "
        "ON facts USING hnsw (embedding vector_cosine_ops) "
        "WITH (m=16, ef_construction=64)"
    )
