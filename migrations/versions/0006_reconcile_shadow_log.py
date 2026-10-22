"""Add the reconcile shadow log.

Records what the reconcile step would have done while the application runs with
BRAIN_RECONCILE_MODE=shadow, so supersession decisions can be audited on real
data before the step is trusted to retire facts.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS reconcile_shadow_log (
            id            BIGSERIAL PRIMARY KEY,
            workspace_id  TEXT NOT NULL,
            team_id       TEXT,
            owner_id      TEXT,
            new_summary   TEXT,
            decision      JSONB NOT NULL,
            candidates    JSONB NOT NULL DEFAULT '[]',
            ts            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS reconcile_shadow_ws
            ON reconcile_shadow_log(workspace_id, ts DESC)
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS reconcile_shadow_log")
