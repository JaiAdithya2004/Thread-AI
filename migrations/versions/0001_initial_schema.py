"""Initial schema — all tables, indexes, RLS policies, and constraints.

This is the canonical source of truth for the production database schema.
db_pg.py's migrate() function remains for dev/bypass mode; this migration
governs production deployments.

Revision ID: 0001
Revises:
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Embedding dimension — must match brain/config.py EMBED_DIM
EMBED_DIM = 1536


def upgrade() -> None:
    # ── Extensions ────────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    # ── teams ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id            TEXT NOT NULL,
            workspace_id  TEXT NOT NULL,
            name          TEXT NOT NULL,
            description   TEXT NOT NULL DEFAULT '',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id, workspace_id)
        );
    """)

    # ── episodes ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id            TEXT PRIMARY KEY,
            workspace_id  TEXT NOT NULL,
            source        TEXT NOT NULL DEFAULT 'unknown',
            external_id   TEXT,
            title         TEXT,
            body          TEXT,
            author        TEXT,
            url           TEXT,
            occurred_at   TIMESTAMPTZ,
            content_hash  TEXT,
            acl           TEXT NOT NULL DEFAULT 'everyone',
            team_id       TEXT NOT NULL DEFAULT 'shared',
            extracted     INTEGER NOT NULL DEFAULT 0
        );
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS episodes_ws_hash
            ON episodes (workspace_id, content_hash);
    """)

    # ── facts ─────────────────────────────────────────────────────────────────
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS facts (
            id             TEXT PRIMARY KEY,
            workspace_id   TEXT NOT NULL,
            subject        TEXT,
            predicate      TEXT,
            object         TEXT,
            summary        TEXT,
            episode_id     TEXT REFERENCES episodes(id) ON DELETE SET NULL,
            acl            TEXT NOT NULL DEFAULT 'everyone',
            confidence     REAL NOT NULL DEFAULT 0.6
                               CONSTRAINT facts_confidence_range
                               CHECK (confidence >= 0.0 AND confidence <= 1.0),
            introduced_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            invalidated_at TIMESTAMPTZ,
            source_kind    TEXT NOT NULL DEFAULT 'llm'
                               CONSTRAINT facts_source_kind_check
                               CHECK (source_kind IN ('llm', 'human', 'agent', 'ai-tool')),
            team_id        TEXT NOT NULL DEFAULT 'shared',
            visibility     TEXT NOT NULL DEFAULT 'team'
                               CONSTRAINT facts_visibility_check
                               CHECK (visibility IN ('team', 'org', 'private')),
            embedding      vector({EMBED_DIM}),
            fts_vec        tsvector GENERATED ALWAYS AS (
                               to_tsvector('english',
                                   coalesce(summary,'') || ' ' ||
                                   coalesce(subject,'') || ' ' ||
                                   coalesce(object,''))
                           ) STORED
        );
    """)
    # HNSW index for approximate nearest-neighbour vector search
    op.execute("""
        CREATE INDEX IF NOT EXISTS facts_embedding_hnsw
            ON facts USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
    """)
    # GIN index for full-text search
    op.execute("""
        CREATE INDEX IF NOT EXISTS facts_fts_gin
            ON facts USING gin (fts_vec);
    """)
    # Workspace lookup
    op.execute("""
        CREATE INDEX IF NOT EXISTS facts_ws_idx ON facts (workspace_id);
    """)

    # ── fact_edges ────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS fact_edges (
            id           BIGSERIAL PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            src_fact     TEXT REFERENCES facts(id) ON DELETE CASCADE,
            dst_fact     TEXT REFERENCES facts(id) ON DELETE CASCADE,
            type         TEXT NOT NULL
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS fact_edges_src ON fact_edges (workspace_id, src_fact);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS fact_edges_dst ON fact_edges (workspace_id, dst_fact);
    """)

    # ── ledger ────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id           BIGSERIAL PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id      TEXT,
            ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
            query        TEXT,
            surface      TEXT
                             CONSTRAINT ledger_surface_check
                             CHECK (surface IS NULL OR surface IN
                                 ('mcp', 'web', 'hook-inject', 'history', 'api', 'hook-harvest')),
            fact_ids     TEXT,
            context_sent TEXT,
            team_id      TEXT
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ledger_ws_ts ON ledger (workspace_id, ts DESC);
    """)

    # ── accounts ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            type       TEXT NOT NULL CHECK (type IN ('individual', 'company')),
            name       TEXT NOT NULL,
            domain     TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)

    # ── memberships ───────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS memberships (
            id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            user_id    TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            role       TEXT NOT NULL DEFAULT 'employee'
                           CHECK (role IN ('owner', 'admin', 'employee')),
            team_id    TEXT,
            status     TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('invited', 'active', 'disabled')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (user_id, account_id, team_id)
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS memberships_user ON memberships(user_id);")
    op.execute("CREATE INDEX IF NOT EXISTS memberships_acct ON memberships(account_id);")

    # ── invitations ───────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS invitations (
            id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            email      TEXT NOT NULL,
            team_ids   TEXT NOT NULL DEFAULT '[]',
            role       TEXT NOT NULL DEFAULT 'employee',
            token      TEXT NOT NULL UNIQUE,
            invited_by TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'accepted', 'expired')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS invitations_token ON invitations(token);")
    op.execute("CREATE INDEX IF NOT EXISTS invitations_email ON invitations(email);")

    # ── conversations ─────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            user_id    TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            team_id    TEXT,
            title      TEXT NOT NULL DEFAULT 'New conversation',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS conv_user ON conversations(user_id, account_id);")

    # ── messages ──────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content         TEXT NOT NULL,
            cited_fact_ids  JSONB NOT NULL DEFAULT '[]',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS msg_conv ON messages(conversation_id);")

    # ── usage_events ──────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id                BIGSERIAL PRIMARY KEY,
            account_id        TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            user_id           TEXT NOT NULL,
            team_id           TEXT,
            task              TEXT,
            model             TEXT,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd          NUMERIC(10,6) NOT NULL DEFAULT 0,
            customer_borne    BOOLEAN NOT NULL DEFAULT FALSE,
            ts                TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS usage_acct ON usage_events(account_id, ts);")

    # ── integration_usage ─────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS integration_usage (
            id                 BIGSERIAL PRIMARY KEY,
            account_id         TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            provider           TEXT NOT NULL,
            connected_accounts INTEGER NOT NULL DEFAULT 0,
            api_calls          INTEGER NOT NULL DEFAULT 0,
            backfill_items     INTEGER NOT NULL DEFAULT 0,
            period             DATE NOT NULL DEFAULT CURRENT_DATE,
            UNIQUE (account_id, provider, period)
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS intusage_acct
            ON integration_usage(account_id, period);
    """)

    # ── plans ─────────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL UNIQUE,
            monthly_credits INTEGER NOT NULL DEFAULT 500,
            max_seats       INTEGER NOT NULL DEFAULT 1,
            max_connectors  INTEGER NOT NULL DEFAULT 0,
            max_backfill    INTEGER NOT NULL DEFAULT 500,
            price_usd_month NUMERIC(8,2) NOT NULL DEFAULT 0
        );
    """)
    # Seed default tiers
    op.execute("""
        INSERT INTO plans
            (id, name, monthly_credits, max_seats, max_connectors, max_backfill, price_usd_month)
        VALUES
            ('plan_free',     'free',       500,   1,   0,    500,   0.00),
            ('plan_team',     'team',      5000,  20,   5,  10000,  49.00),
            ('plan_business', 'business', 50000, 100,  20, 100000, 299.00)
        ON CONFLICT (id) DO NOTHING;
    """)

    # ── subscriptions ─────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id           TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            account_id   TEXT NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
            plan_id      TEXT NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
            status       TEXT NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'past_due', 'cancelled')),
            started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            renews_at    TIMESTAMPTZ,
            cancelled_at TIMESTAMPTZ
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS subs_acct ON subscriptions(account_id);")

    # ── workspace_llm_keys ────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS workspace_llm_keys (
            id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            provider   TEXT NOT NULL,
            api_key    TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by TEXT NOT NULL,
            UNIQUE (account_id, provider)
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS wlk_acct ON workspace_llm_keys(account_id);")

    # ── dead_letter_jobs ──────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS dead_letter_jobs (
            id          BIGSERIAL PRIMARY KEY,
            job_name    TEXT NOT NULL,
            kwargs      JSONB NOT NULL DEFAULT '{}',
            error       TEXT NOT NULL,
            attempts    INTEGER NOT NULL DEFAULT 0,
            failed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            replayed_at TIMESTAMPTZ,
            status      TEXT NOT NULL DEFAULT 'failed'
                            CHECK (status IN ('failed', 'replayed', 'discarded'))
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS dlq_status
            ON dead_letter_jobs(status, failed_at DESC);
    """)

    # ── Row-Level Security ────────────────────────────────────────────────────
    # Workspace-scoped tables: isolate by brain.workspace_id session variable
    for table in ("episodes", "facts", "fact_edges", "ledger", "teams"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"DROP POLICY IF EXISTS ws_isolation ON {table};")
        op.execute(f"""
            CREATE POLICY ws_isolation ON {table}
                USING (workspace_id = current_setting('brain.workspace_id', true));
        """)

    # User-scoped tables: FORCE RLS so even the table owner cannot bypass
    for table in ("conversations", "messages"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table};")

    op.execute("""
        CREATE POLICY user_isolation ON conversations
            USING (user_id = current_setting('brain.user_id', true));
    """)
    op.execute("""
        CREATE POLICY user_isolation ON messages
            USING (
                conversation_id IN (
                    SELECT id FROM conversations
                    WHERE user_id = current_setting('brain.user_id', true)
                )
            );
    """)

    # Grant DML to authenticated role (Supabase pattern)
    for table in ("conversations", "messages"):
        op.execute(f"""
            DO $$
            BEGIN
                GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO authenticated;
            EXCEPTION WHEN undefined_object THEN
                NULL;  -- 'authenticated' role may not exist on local Postgres
            END
            $$;
        """)


def downgrade() -> None:
    """Drop all tables in reverse dependency order."""
    # Remove RLS policies before dropping tables
    for table in ("conversations", "messages"):
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    for table in ("episodes", "facts", "fact_edges", "ledger", "teams"):
        op.execute(f"DROP POLICY IF EXISTS ws_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    # Drop tables in reverse FK dependency order
    op.execute("DROP TABLE IF EXISTS dead_letter_jobs CASCADE;")
    op.execute("DROP TABLE IF EXISTS workspace_llm_keys CASCADE;")
    op.execute("DROP TABLE IF EXISTS subscriptions CASCADE;")
    op.execute("DROP TABLE IF EXISTS plans CASCADE;")
    op.execute("DROP TABLE IF EXISTS integration_usage CASCADE;")
    op.execute("DROP TABLE IF EXISTS usage_events CASCADE;")
    op.execute("DROP TABLE IF EXISTS messages CASCADE;")
    op.execute("DROP TABLE IF EXISTS conversations CASCADE;")
    op.execute("DROP TABLE IF EXISTS invitations CASCADE;")
    op.execute("DROP TABLE IF EXISTS memberships CASCADE;")
    op.execute("DROP TABLE IF EXISTS accounts CASCADE;")
    op.execute("DROP TABLE IF EXISTS ledger CASCADE;")
    op.execute("DROP TABLE IF EXISTS fact_edges CASCADE;")
    op.execute("DROP TABLE IF EXISTS facts CASCADE;")
    op.execute("DROP TABLE IF EXISTS episodes CASCADE;")
    op.execute("DROP TABLE IF EXISTS teams CASCADE;")
