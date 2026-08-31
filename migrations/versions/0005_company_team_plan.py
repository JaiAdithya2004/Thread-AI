"""Move existing company accounts off the 1-seat free plan.

Every account was provisioned onto plan_free regardless of type. Its single
seat is taken by the admin created at signup, so a company account could never
invite the members that define it — the first invite always returned 402.

New accounts now get the right tier at provisioning time; this backfills the
ones created before that fix. Only rows still on plan_free are touched, so an
account that was deliberately upgraded or downgraded is left alone.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        UPDATE subscriptions s
           SET plan_id = 'plan_team'
          FROM accounts a
         WHERE a.id = s.account_id
           AND a.type = 'company'
           AND s.plan_id = 'plan_free'
           AND s.status = 'active'
    """)


def downgrade():
    # Not reversible with fidelity: a company account legitimately on plan_team
    # is indistinguishable from one this migration moved. Reverting would
    # downgrade paying customers, so this is deliberately a no-op.
    pass
