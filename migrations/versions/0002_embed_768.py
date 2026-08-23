"""Add 768-dim Gemini embedding column alongside existing 384 store.

Phase 2 of gemini-embedding-001 migration. The 384 column (embedding) remains
live and serves search until Phase 4 cutover. Both columns coexist during migration.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    # Add 768-dim vector column — nullable so existing rows don't need immediate backfill
    op.execute("""
        ALTER TABLE facts
        ADD COLUMN IF NOT EXISTS fact_vec_768 vector(768)
    """)
    # Index will be built in Phase 4 after backfill completes
    # (building HNSW on a partially-filled column is wasteful)


def downgrade():
    op.execute("DROP INDEX IF EXISTS facts_vec_768_idx")
    op.execute("ALTER TABLE facts DROP COLUMN IF EXISTS fact_vec_768")
