"""Build HNSW index on fact_vec_768.

Run AFTER reembed_gemini.py has populated all 768 vectors.
Phase 4 of gemini-embedding-001 migration.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    # End Alembic's implicit transaction, then use autocommit.
    connection = op.get_bind()
    connection.execute(op.inline_literal("COMMIT"))
    connection.execution_options(isolation_level="AUTOCOMMIT").execute(
        op.inline_literal(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS facts_vec_768_idx "
            "ON facts USING hnsw (fact_vec_768 vector_cosine_ops) "
            "WITH (m=16, ef_construction=64)"
        )
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS facts_vec_768_idx")
