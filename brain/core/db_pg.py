"""
Postgres + pgvector backend for Company Brain.
Uses psycopg2 (sync) so the rest of the codebase needs zero async changes.
"""
import datetime
import json
import sys
import uuid
import threading

# ---------------------------------------------------------------------------
# Connection pool — reuse connections instead of cold-opening one per request
# ---------------------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        import psycopg2.pool
        import psycopg2.extras
        import os
        url = os.getenv("DATABASE_URL", "postgresql://brain:brain_local@localhost:5432/brain")
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=url,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool


class _PooledConn:
    """
    Thin proxy around a psycopg2 connection that returns it to the pool
    on close() instead of actually closing it.

    psycopg2's connection.close is a read-only C attribute so we can't
    monkey-patch it directly; this wrapper intercepts it at the Python level.
    """
    __slots__ = ("_conn", "_pool", "_real_close")

    def __init__(self, conn, pool):
        object.__setattr__(self, "_conn",       conn)
        object.__setattr__(self, "_pool",       pool)
        object.__setattr__(self, "_real_close", conn.close)

    # ── intercept close() ──────────────────────────────────────────────────────
    def close(self):
        conn = object.__getattribute__(self, "_conn")
        pool = object.__getattribute__(self, "_pool")
        real = object.__getattribute__(self, "_real_close")
        try:
            if not conn.closed:
                conn.rollback()
            pool.putconn(conn)
        except Exception:
            try:
                real()
            except Exception:
                pass

    # ── forward everything else to the real connection ─────────────────────────
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        if name in ("_conn", "_pool", "_real_close"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)


def connect():
    """Get a connection from the pool. conn.close() returns it to the pool.

    A pooled connection can be dead on arrival (closed out-of-band, or dropped
    server-side), so validate before handing it out and discard bad ones.
    """
    import psycopg2
    pool = _get_pool()
    last_err = None
    for _ in range(3):
        raw = pool.getconn()
        try:
            if raw.closed:
                raise psycopg2.InterfaceError("connection already closed")
            raw.autocommit = False
            # Clear any statement timeout the pooler may have left, and prove the
            # socket is actually usable before returning the connection.
            cur = raw.cursor()
            cur.execute("SET statement_timeout = 0")
            raw.commit()
            cur.close()
            return _PooledConn(raw, pool)
        except psycopg2.Error as _e:
            last_err = _e
            # Drop it from the pool entirely — putconn() would recycle the corpse.
            try:
                pool.putconn(raw, close=True)
            except Exception:
                pass
    raise last_err or psycopg2.InterfaceError("could not obtain a live connection")


# Guard so migrate() runs once per process, not once per request
_migrated = False


def migrate(conn):
    """Idempotent schema setup: extensions, tables, indexes, RLS. Runs once per process."""
    global _migrated
    if _migrated:
        return
    cur = conn.cursor()

    # Extensions
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    # Teams
    cur.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id            TEXT NOT NULL,
            workspace_id  TEXT NOT NULL,
            name          TEXT NOT NULL,
            description   TEXT DEFAULT '',
            created_at    TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (id, workspace_id)
        );
    """)

    # Episodes
    cur.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id            TEXT PRIMARY KEY,
            workspace_id  TEXT NOT NULL,
            source        TEXT,
            external_id   TEXT,
            title         TEXT,
            body          TEXT,
            author        TEXT,
            url           TEXT,
            occurred_at   TIMESTAMPTZ,
            content_hash  TEXT,
            acl           TEXT DEFAULT 'everyone',
            team_id       TEXT DEFAULT 'shared',
            extracted     INTEGER DEFAULT 0
        );
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS episodes_ws_hash
            ON episodes (workspace_id, content_hash);
    """)

    # Facts
    cur.execute("""
        CREATE TABLE IF NOT EXISTS facts (
            id            TEXT PRIMARY KEY,
            workspace_id  TEXT NOT NULL,
            subject       TEXT,
            predicate     TEXT,
            object        TEXT,
            summary       TEXT,
            episode_id    TEXT REFERENCES episodes(id) ON DELETE SET NULL,
            acl           TEXT DEFAULT 'everyone',
            confidence    REAL,
            introduced_at TIMESTAMPTZ DEFAULT now(),
            invalidated_at TIMESTAMPTZ,
            source_kind   TEXT DEFAULT 'llm',
            team_id       TEXT DEFAULT 'shared',
            visibility    TEXT DEFAULT 'team',
            fact_vec_768  vector(768),
            fts_vec       tsvector GENERATED ALWAYS AS (
                              to_tsvector('english',
                                  coalesce(summary,'') || ' ' ||
                                  coalesce(subject,'') || ' ' ||
                                  coalesce(object,''))
                          ) STORED
        );
    """)
    # Ensure 768-dim column exists on existing DBs (idempotent upgrade).
    conn.commit()
    cur.execute("""
        ALTER TABLE facts
        ADD COLUMN IF NOT EXISTS fact_vec_768 vector(768)
    """)
    # NOTE: The HNSW index (facts_vec_768_idx) is NOT created here — building it
    # on app startup blocks all requests. Run scripts/rebuild_index.py once after
    # reembed_gemini.py finishes to create it. Search still works without the index
    # (falls back to sequential scan) until then.
    # GIN index for full-text
    cur.execute("""
        CREATE INDEX IF NOT EXISTS facts_fts_gin
            ON facts USING gin (fts_vec);
    """)
    # workspace lookup
    cur.execute("""
        CREATE INDEX IF NOT EXISTS facts_ws_idx ON facts (workspace_id);
    """)

    # Fact edges
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fact_edges (
            id            BIGSERIAL PRIMARY KEY,
            workspace_id  TEXT NOT NULL,
            src_fact      TEXT,
            dst_fact      TEXT,
            type          TEXT
        );
    """)

    # Ledger
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id            BIGSERIAL PRIMARY KEY,
            workspace_id  TEXT NOT NULL,
            user_id       TEXT,
            ts            TIMESTAMPTZ DEFAULT now(),
            query         TEXT,
            surface       TEXT,
            fact_ids      TEXT,
            context_sent  TEXT,
            team_id       TEXT
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ledger_ws ON ledger(workspace_id, id DESC);")
    cur.execute("CREATE INDEX IF NOT EXISTS ledger_team ON ledger(workspace_id, team_id, id DESC);")

    # ── Auth / account tables ──────────────────────────────────────────────────

    # accounts — one row per workspace (individual or company)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id           TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            type         TEXT NOT NULL CHECK (type IN ('individual','company')),
            name         TEXT NOT NULL,
            domain       TEXT,
            created_at   TIMESTAMPTZ DEFAULT now()
        );
    """)

    # memberships — links Supabase users to accounts + role + department
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memberships (
            id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            user_id    TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            role       TEXT NOT NULL DEFAULT 'employee'
                           CHECK (role IN ('owner','admin','employee')),
            team_id    TEXT,
            status     TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('invited','active','disabled')),
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (user_id, account_id, team_id)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS memberships_user ON memberships(user_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS memberships_acct ON memberships(account_id);")

    # invitations — tokenized invite links sent by admins
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invitations (
            id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            email       TEXT NOT NULL,
            team_ids    TEXT NOT NULL DEFAULT '[]',
            role        TEXT NOT NULL DEFAULT 'employee',
            token       TEXT NOT NULL UNIQUE,
            invited_by  TEXT NOT NULL,
            expires_at  TIMESTAMPTZ NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','accepted','expired')),
            created_at  TIMESTAMPTZ DEFAULT now()
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS invitations_token ON invitations(token);")
    cur.execute("CREATE INDEX IF NOT EXISTS invitations_email ON invitations(email);")

    # conversations — per-user chat threads (strictly private)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            user_id    TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            team_id    TEXT,
            title      TEXT NOT NULL DEFAULT 'New conversation',
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS conv_user ON conversations(user_id, account_id);")

    # messages — individual Q/A turns inside a conversation
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
            content         TEXT NOT NULL,
            cited_fact_ids  JSONB DEFAULT '[]',
            created_at      TIMESTAMPTZ DEFAULT now()
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS msg_conv ON messages(conversation_id);")

    # usage_events — metadata only, NEVER stores query/answer content
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id                BIGSERIAL PRIMARY KEY,
            account_id        TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            user_id           TEXT NOT NULL,
            team_id           TEXT,
            task              TEXT,
            model             TEXT,
            prompt_tokens     INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cost_usd          NUMERIC(10,6) DEFAULT 0,
            ts                TIMESTAMPTZ DEFAULT now()
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS usage_acct ON usage_events(account_id, ts);")

    # Add customer_borne column idempotently (Phase 2 BYO-key flag)
    cur.execute("""
        ALTER TABLE usage_events
            ADD COLUMN IF NOT EXISTS customer_borne BOOLEAN DEFAULT FALSE;
    """)

    # integration_usage — COGS tracker for connector API calls (never exposes vendor name to users)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS integration_usage (
            id                  BIGSERIAL PRIMARY KEY,
            account_id          TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            provider            TEXT NOT NULL,
            connected_accounts  INTEGER DEFAULT 0,
            api_calls           INTEGER DEFAULT 0,
            backfill_items      INTEGER DEFAULT 0,
            period              DATE NOT NULL DEFAULT CURRENT_DATE,
            UNIQUE (account_id, provider, period)
        );
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS intusage_acct
            ON integration_usage(account_id, period);
    """)

    # ── Phase 2: plans, subscriptions, BYO-key ────────────────────────────────

    # plans — product tiers seeded at startup; never written by customer-facing code
    cur.execute("""
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
    # Seed default tiers idempotently
    cur.execute("""
        INSERT INTO plans (id, name, monthly_credits, max_seats, max_connectors, max_backfill, price_usd_month)
        VALUES
            ('plan_free',     'free',     500,   1,  0,    500,  0.00),
            ('plan_team',     'team',     5000,  20, 5,    10000, 49.00),
            ('plan_business', 'business', 50000, 100, 20,  100000, 299.00)
        ON CONFLICT (id) DO NOTHING;
    """)

    # subscriptions — one active row per account
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            account_id  TEXT NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
            plan_id     TEXT NOT NULL REFERENCES plans(id),
            status      TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','past_due','cancelled')),
            started_at  TIMESTAMPTZ DEFAULT now(),
            renews_at   TIMESTAMPTZ,
            cancelled_at TIMESTAMPTZ
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS subs_acct ON subscriptions(account_id);")

    # workspace_llm_keys — BYO customer LLM keys. api_key holds Fernet ciphertext
    # (see _encrypt_secret); the master key lives in BRAIN_KEY_ENCRYPTION_KEY and
    # is never stored in this database.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS workspace_llm_keys (
            id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            provider    TEXT NOT NULL,
            api_key     TEXT NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT now(),
            created_by  TEXT NOT NULL,
            UNIQUE (account_id, provider)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS wlk_acct ON workspace_llm_keys(account_id);")

    # ── Phase 3: idempotent constraint additions ──────────────────────────────
    # Use DO blocks with EXISTS checks — ADD CONSTRAINT IF NOT EXISTS is not
    # valid Postgres syntax. Savepoints ensure a failed check doesn't roll
    # back the entire migration transaction.
    # Allowed value lists must match what the code actually writes:
    #   visibility — extract.py/api.py/cli.py emit only 'team' or 'shared'
    #   surface    — every call site that reaches db.log(), plus 'test' for the suite
    _constraints = [
        ("facts", "facts_confidence_range",
         "CHECK (confidence >= 0.0 AND confidence <= 1.0)"),
        ("facts", "facts_source_kind_check",
         "CHECK (source_kind IN ('llm', 'human', 'agent', 'ai-tool'))"),
        ("facts", "facts_visibility_check",
         "CHECK (visibility IN ('team', 'shared'))"),
        ("ledger", "ledger_surface_check",
         "CHECK (surface IS NULL OR surface IN "
         "('mcp', 'web', 'api', 'slack', 'eval', 'test', "
         "'history', 'hook-inject', 'hook-harvest'))"),
    ]
    _stderr = sys.stderr
    for _tbl, _cname, _cdef in _constraints:
        # Drop first, then re-add: a DB migrated by an older build may carry an
        # outdated definition, and ADD-if-missing would silently keep the stale one.
        cur.execute("SAVEPOINT _add_constraint")
        try:
            cur.execute(f"ALTER TABLE {_tbl} DROP CONSTRAINT IF EXISTS {_cname}")
            cur.execute(f"ALTER TABLE {_tbl} ADD CONSTRAINT {_cname} {_cdef}")
            cur.execute("RELEASE SAVEPOINT _add_constraint")
        except Exception as _e:
            cur.execute("ROLLBACK TO SAVEPOINT _add_constraint")
            cur.execute("RELEASE SAVEPOINT _add_constraint")
            print(f"[migrate] {_cname} constraint: {_e}", file=_stderr)

    # episodes.source — ensure NOT NULL with default for any rows that have NULL source
    cur.execute("SAVEPOINT _src_default")
    try:
        cur.execute("UPDATE episodes SET source = 'unknown' WHERE source IS NULL")
        cur.execute("ALTER TABLE episodes ALTER COLUMN source SET DEFAULT 'unknown'")
        cur.execute("RELEASE SAVEPOINT _src_default")
    except Exception as _e:
        cur.execute("ROLLBACK TO SAVEPOINT _src_default")
        cur.execute("RELEASE SAVEPOINT _src_default")
        print(f"[migrate] episodes.source default: {_e}", file=sys.stderr)

    # Row-Level Security — defense in depth, workspace isolation
    for table in ("episodes", "facts", "fact_edges", "ledger", "teams"):
        cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        cur.execute(f"DROP POLICY IF EXISTS ws_isolation ON {table};")
        cur.execute(f"""
            CREATE POLICY ws_isolation ON {table}
                USING (workspace_id = current_setting('brain.workspace_id', true));
        """)

    # RLS on conversations + messages: strictly user-scoped, NO admin override
    # FORCE RLS ensures even the table owner (postgres) cannot bypass policies
    for table in ("conversations", "messages"):
        cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        cur.execute(f"DROP POLICY IF EXISTS user_isolation ON {table};")

    cur.execute("""
        CREATE POLICY user_isolation ON conversations
            USING (user_id = current_setting('brain.user_id', true));
    """)
    cur.execute("""
        CREATE POLICY user_isolation ON messages
            USING (
                conversation_id IN (
                    SELECT id FROM conversations
                    WHERE user_id = current_setting('brain.user_id', true)
                )
            );
    """)

    # Supabase ships an 'authenticated' role; plain Postgres (CI, local) does not.
    # Create it when missing so SET LOCAL ROLE actually sheds BYPASSRLS — without
    # it queries keep running as the owner and FORCE RLS is silently ineffective.
    cur.execute("SAVEPOINT _mk_authenticated")
    try:
        cur.execute("""
            DO $do$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated')
                THEN CREATE ROLE authenticated NOLOGIN NOBYPASSRLS;
                END IF;
            END $do$;
        """)
        cur.execute("RELEASE SAVEPOINT _mk_authenticated")
    except Exception as _e:
        cur.execute("ROLLBACK TO SAVEPOINT _mk_authenticated")
        cur.execute("RELEASE SAVEPOINT _mk_authenticated")
        # insufficient privilege to create roles — RLS still enforced for app roles
        print(f"[migrate] authenticated role: {_e}", file=sys.stderr)

    # Grant DML to Supabase's non-bypassrls roles so SET LOCAL ROLE authenticated works.
    # Write access stays confined to the caller's own chat tables; the lookup tables
    # below are read-only, since request handlers must resolve the caller's account
    # after dropping to this role.
    _grants = [
        ("conversations", "SELECT, INSERT, UPDATE, DELETE"),
        ("messages",      "SELECT, INSERT, UPDATE, DELETE"),
        ("memberships",   "SELECT"),
        ("accounts",      "SELECT"),
        ("teams",         "SELECT"),
    ]
    for _i, (table, privs) in enumerate(_grants):
        cur.execute(f"SAVEPOINT _grant_{_i}")
        try:
            cur.execute(f"GRANT {privs} ON {table} TO authenticated;")
            cur.execute(f"RELEASE SAVEPOINT _grant_{_i}")
        except Exception:
            cur.execute(f"ROLLBACK TO SAVEPOINT _grant_{_i}")
            cur.execute(f"RELEASE SAVEPOINT _grant_{_i}")
            # role may not exist on local Postgres installs — non-fatal

    # Dead-letter queue — poison jobs that exhausted all retries land here
    cur.execute("""
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
    cur.execute("""
        CREATE INDEX IF NOT EXISTS dlq_status ON dead_letter_jobs(status, failed_at DESC);
    """)

    # ── Phase 1: Slack OAuth + channel mapping tables ─────────────────────────

    # Slack OAuth per workspace
    cur.execute("""
        CREATE TABLE IF NOT EXISTS slack_workspace_integrations (
            id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            slack_team_id   TEXT NOT NULL UNIQUE,
            slack_team_name TEXT NOT NULL,
            bot_token       TEXT NOT NULL,
            bot_user_id     TEXT NOT NULL,
            installed_by    TEXT NOT NULL,
            created_at      TIMESTAMPTZ DEFAULT now()
        );
    """)

    # Maps Slack user ID -> our user_id
    cur.execute("""
        CREATE TABLE IF NOT EXISTS slack_user_links (
            id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            user_id         TEXT NOT NULL,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            slack_user_id   TEXT NOT NULL,
            slack_team_id   TEXT NOT NULL,
            slack_email     TEXT,
            linked_at       TIMESTAMPTZ DEFAULT now(),
            UNIQUE (slack_user_id, slack_team_id)
        );
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS slack_links_user
            ON slack_user_links(user_id);
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS slack_links_slack_user
            ON slack_user_links(slack_user_id, slack_team_id);
    """)

    # Channel -> department mapping (optional, admin-configured)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS slack_channel_mappings (
            id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            account_id      TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            slack_team_id   TEXT NOT NULL,
            channel_id      TEXT NOT NULL,
            channel_name    TEXT NOT NULL,
            team_id         TEXT NOT NULL,
            sync_members    BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ DEFAULT now(),
            UNIQUE (account_id, channel_id)
        );
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS slack_channel_map_acct
            ON slack_channel_mappings(account_id, slack_team_id);
    """)

    # ── Connector sync state (polling watermarks + state machine) ────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS connector_sync_state (
            workspace_id        TEXT NOT NULL,
            source              TEXT NOT NULL,
            last_synced_at      TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01T00:00:00Z',
            last_count          INTEGER NOT NULL DEFAULT 0,
            status              TEXT NOT NULL DEFAULT 'idle'
                                    CHECK (status IN ('idle', 'running', 'failed')),
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error          TEXT,
            locked_until        TIMESTAMPTZ,
            last_run_job_id     TEXT,
            last_run_started_at TIMESTAMPTZ,
            PRIMARY KEY (workspace_id, source)
        );
    """)
    # Migrate existing rows: add new columns if the table already existed
    for col, defn in [
        ("status",               "TEXT NOT NULL DEFAULT 'idle'"),
        ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
        ("last_error",           "TEXT"),
        ("locked_until",         "TIMESTAMPTZ"),
        ("last_run_job_id",      "TEXT"),
        ("last_run_started_at",  "TIMESTAMPTZ"),
    ]:
        cur.execute("SAVEPOINT _css_col")
        try:
            cur.execute(f"ALTER TABLE connector_sync_state ADD COLUMN IF NOT EXISTS {col} {defn}")
            cur.execute("RELEASE SAVEPOINT _css_col")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT _css_col")
            cur.execute("RELEASE SAVEPOINT _css_col")
    # Index must be created after columns exist (for existing DBs being migrated)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS css_status_idx
            ON connector_sync_state(status, locked_until);
    """)

    # ── External-id supersession on episodes ──────────────────────────────────
    cur.execute("""
        ALTER TABLE episodes
            ADD COLUMN IF NOT EXISTS supersedes_episode_id TEXT REFERENCES episodes(id)
                ON DELETE SET NULL;
    """)
    cur.execute("""
        ALTER TABLE episodes
            ADD COLUMN IF NOT EXISTS ingest_trigger TEXT DEFAULT 'poll'
                CHECK (ingest_trigger IN ('webhook','poll','backfill','reconcile','manual'));
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS episodes_ext_id
            ON episodes(workspace_id, external_id)
            WHERE external_id IS NOT NULL;
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS episodes_supersedes
            ON episodes(supersedes_episode_id)
            WHERE supersedes_episode_id IS NOT NULL;
    """)

    # ── Webhook routing: entity_id -> workspace ───────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS connector_webhook_routes (
            entity_id     TEXT PRIMARY KEY,
            workspace_id  TEXT NOT NULL,
            account_id    TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS webhook_subscriptions (
            id                   TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
            workspace_id         TEXT NOT NULL,
            source               TEXT NOT NULL,
            app_slug             TEXT NOT NULL,
            composio_trigger_id  TEXT,
            webhook_url          TEXT NOT NULL,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            active               BOOLEAN NOT NULL DEFAULT TRUE
        );
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS ws_sub_workspace
            ON webhook_subscriptions(workspace_id, source, active);
    """)

    conn.commit()
    cur.close()
    _migrated = True

    # Warn if HNSW vector index is missing — searches degrade to sequential scan
    try:
        chk = conn.cursor()
        chk.execute("""
            SELECT 1 FROM pg_indexes
            WHERE tablename='facts' AND indexname='facts_vec_768_idx'
        """)
        if not chk.fetchone():
            print(
                "[thread] WARNING: HNSW vector index not found on facts.fact_vec_768. "
                "Vector search will use a slow sequential scan. "
                "Run: python scripts/rebuild_index.py",
                file=sys.stderr,
            )
        chk.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Connector sync state helpers
# ---------------------------------------------------------------------------

def get_sync_state(conn, workspace_id: str, source: str) -> dict:
    """Return full sync state for a workspace+source."""
    cur = conn.cursor()
    cur.execute(
        """SELECT last_synced_at, last_count, status, consecutive_failures,
                  last_error, locked_until, last_run_job_id, last_run_started_at
           FROM connector_sync_state
           WHERE workspace_id=%s AND source=%s""",
        (workspace_id, source),
    )
    row = cur.fetchone()
    cur.close()
    if row:
        return dict(row)
    return {
        "last_synced_at": None, "last_count": 0, "status": "idle",
        "consecutive_failures": 0, "last_error": None,
        "locked_until": None, "last_run_job_id": None, "last_run_started_at": None,
    }


def set_sync_state(conn, workspace_id: str, source: str, last_count: int = 0):
    """Mark a sync successful: update watermark to now(), reset failure counters."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO connector_sync_state
            (workspace_id, source, last_synced_at, last_count, status,
             consecutive_failures, last_error, locked_until)
        VALUES (%s, %s, now(), %s, 'idle', 0, NULL, NULL)
        ON CONFLICT (workspace_id, source) DO UPDATE SET
            last_synced_at       = now(),
            last_count           = EXCLUDED.last_count,
            status               = 'idle',
            consecutive_failures = 0,
            last_error           = NULL,
            locked_until         = NULL
        """,
        (workspace_id, source, last_count),
    )
    conn.commit()
    cur.close()


def set_sync_state_running(conn, workspace_id: str, source: str, job_id: str = ""):
    """Mark a sync as in-flight."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO connector_sync_state
            (workspace_id, source, status, last_run_job_id, last_run_started_at)
        VALUES (%s, %s, 'running', %s, now())
        ON CONFLICT (workspace_id, source) DO UPDATE SET
            status               = 'running',
            last_run_job_id      = EXCLUDED.last_run_job_id,
            last_run_started_at  = now()
        """,
        (workspace_id, source, job_id),
    )
    conn.commit()
    cur.close()


def set_sync_state_failed(conn, workspace_id: str, source: str, error: str = ""):
    """Mark a sync failed and compute exponential backoff for locked_until."""
    import datetime
    cur = conn.cursor()
    # Read current failure count
    cur.execute(
        "SELECT consecutive_failures FROM connector_sync_state "
        "WHERE workspace_id=%s AND source=%s",
        (workspace_id, source),
    )
    row = cur.fetchone()
    failures = (row["consecutive_failures"] if row else 0) + 1
    backoff_s = min(2 ** failures * 60, 3600)
    locked_until = datetime.datetime.utcnow() + datetime.timedelta(seconds=backoff_s)
    cur.execute(
        """
        INSERT INTO connector_sync_state
            (workspace_id, source, status, consecutive_failures, last_error, locked_until)
        VALUES (%s, %s, 'failed', %s, %s, %s)
        ON CONFLICT (workspace_id, source) DO UPDATE SET
            status               = 'failed',
            consecutive_failures = EXCLUDED.consecutive_failures,
            last_error           = EXCLUDED.last_error,
            locked_until         = EXCLUDED.locked_until
        """,
        (workspace_id, source, failures, error[:2000], locked_until),
    )
    conn.commit()
    cur.close()


def reset_stuck_sync(conn, workspace_id: str, source: str):
    """Reset a sync stuck in 'running' state back to idle."""
    cur = conn.cursor()
    cur.execute(
        """UPDATE connector_sync_state SET status='idle', last_run_job_id=NULL
           WHERE workspace_id=%s AND source=%s AND status='running'""",
        (workspace_id, source),
    )
    conn.commit()
    cur.close()


def lookup_webhook_route(conn, entity_id: str) -> dict:
    """Resolve entity_id -> {workspace_id, account_id}. Returns None if not found."""
    cur = conn.cursor()
    cur.execute(
        "SELECT workspace_id, account_id FROM connector_webhook_routes WHERE entity_id=%s",
        (entity_id,),
    )
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def upsert_webhook_route(conn, entity_id: str, workspace_id: str, account_id: str):
    """Register or update the entity_id -> workspace mapping for webhook routing."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO connector_webhook_routes (entity_id, workspace_id, account_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (entity_id) DO UPDATE SET
            workspace_id = EXCLUDED.workspace_id,
            account_id   = EXCLUDED.account_id
        """,
        (entity_id, workspace_id, account_id),
    )
    conn.commit()
    cur.close()


def list_active_workspaces(conn) -> list:
    """Return all workspace_ids that have at least one active fact."""
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT workspace_id FROM facts WHERE invalidated_at IS NULL"
    )
    rows = cur.fetchall()
    cur.close()
    return [r["workspace_id"] for r in rows]


# ---------------------------------------------------------------------------
# RLS helper - set workspace for every connection before queries
# ---------------------------------------------------------------------------

def _set_ws(conn, ws: str):
    cur = conn.cursor()
    cur.execute("SELECT set_config('brain.workspace_id', %s, false)", (ws,))
    cur.close()


def _set_user_id(conn, uid: str):
    """Set brain.user_id session var so RLS policies can read it.
    Also drops to 'authenticated' role so BYPASSRLS privilege is shed.
    Falls back gracefully if 'authenticated' role doesn't exist (local PG).
    """
    cur = conn.cursor()
    try:
        cur.execute("SET LOCAL ROLE authenticated")
    except Exception:
        conn.rollback()
    cur.execute("SELECT set_config('brain.user_id', %s, false)", (uid,))
    cur.close()


# ---------------------------------------------------------------------------
# High-level interface (identical signatures to db.py)
# ---------------------------------------------------------------------------

def save_episode(conn, ws, ep_dict, content_hash=None) -> str:
    _set_ws(conn, ws)
    ep_id = ep_dict.get("id") or str(uuid.uuid4())
    ch = content_hash or ep_dict.get("content_hash")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO episodes
            (id, workspace_id, source, external_id, title, body, author, url,
             occurred_at, content_hash, acl, team_id, extracted)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (workspace_id, content_hash) DO NOTHING
    """, (
        ep_id, ws,
        ep_dict.get("source"), ep_dict.get("external_id"),
        ep_dict.get("title"), ep_dict.get("body"),
        ep_dict.get("author"), ep_dict.get("url"),
        ep_dict.get("occurred_at"), ch,
        ep_dict.get("acl", "everyone"),
        ep_dict.get("team_id", "shared"),
        ep_dict.get("extracted", 0),
    ))
    conn.commit()
    cur.execute(
        "SELECT id FROM episodes WHERE workspace_id=%s AND content_hash=%s LIMIT 1",
        (ws, ch)
    )
    row = cur.fetchone()
    cur.close()
    return row["id"] if row else ep_id


def save_fact(conn, ws, fact_dict, embedding: list) -> str:
    _set_ws(conn, ws)
    fact_id = fact_dict.get("id") or str(uuid.uuid4())
    emb_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]" if embedding else None
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO facts
            (id, workspace_id, subject, predicate, object, summary,
             episode_id, acl, confidence, introduced_at, invalidated_at,
             source_kind, team_id, visibility, fact_vec_768)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET
            subject=EXCLUDED.subject, predicate=EXCLUDED.predicate,
            object=EXCLUDED.object, summary=EXCLUDED.summary,
            confidence=EXCLUDED.confidence, fact_vec_768=EXCLUDED.fact_vec_768,
            invalidated_at=EXCLUDED.invalidated_at, visibility=EXCLUDED.visibility
    """, (
        fact_id, ws,
        fact_dict.get("subject"), fact_dict.get("predicate"),
        fact_dict.get("object"), fact_dict.get("summary"),
        fact_dict.get("episode_id"), fact_dict.get("acl", "everyone"),
        fact_dict.get("confidence"),
        fact_dict.get("introduced_at", datetime.datetime.utcnow().isoformat()),
        fact_dict.get("invalidated_at"),
        fact_dict.get("source_kind", "llm"),
        fact_dict.get("team_id", "shared"),
        fact_dict.get("visibility", "team"),
        emb_str,
    ))
    conn.commit()
    cur.close()
    return fact_id


def add_edge(conn, ws, src: str, dst: str, etype: str) -> None:
    _set_ws(conn, ws)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO fact_edges(workspace_id, src_fact, dst_fact, type) VALUES (%s,%s,%s,%s)",
        (ws, src, dst, etype)
    )
    conn.commit()
    cur.close()


def get_fact(conn, ws, fact_id: str):
    _set_ws(conn, ws)
    cur = conn.cursor()
    cur.execute("SELECT * FROM facts WHERE id=%s AND workspace_id=%s", (fact_id, ws))
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def search_vec(conn, query_embedding: list, n: int = 30, workspace_id: str = None) -> list:
    vec_str = "[" + ",".join(f"{v:.8f}" for v in query_embedding) + "]"
    cur = conn.cursor()
    if workspace_id:
        cur.execute("""
            SELECT id FROM facts
            WHERE fact_vec_768 IS NOT NULL
              AND invalidated_at IS NULL
              AND workspace_id = %s
            ORDER BY fact_vec_768 <=> %s::vector
            LIMIT %s
        """, (workspace_id, vec_str, n))
    else:
        cur.execute("""
            SELECT id FROM facts
            WHERE fact_vec_768 IS NOT NULL
              AND invalidated_at IS NULL
            ORDER BY fact_vec_768 <=> %s::vector
            LIMIT %s
        """, (vec_str, n))
    rows = cur.fetchall()
    cur.close()
    return [r["id"] for r in rows]


def search_fts(conn, query: str, n: int = 30) -> list:
    if not query or not query.strip():
        return []
    cur = conn.cursor()
    cur.execute("""
        SELECT id, ts_rank(fts_vec, plainto_tsquery('english', %s)) AS rank
        FROM facts
        WHERE fts_vec @@ plainto_tsquery('english', %s)
          AND invalidated_at IS NULL
        ORDER BY rank DESC
        LIMIT %s
    """, (query, query, n))
    rows = cur.fetchall()
    cur.close()
    return [r["id"] for r in rows]


def invalidate_fact(conn, ws, fact_id: str, ts: str) -> None:
    _set_ws(conn, ws)
    cur = conn.cursor()
    cur.execute(
        "UPDATE facts SET invalidated_at=%s WHERE id=%s AND workspace_id=%s",
        (ts, fact_id, ws)
    )
    conn.commit()
    cur.close()


def ensure_team(conn, ws, team_id, name, description=""):
    _set_ws(conn, ws)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO teams(id, workspace_id, name, description, created_at)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (id, workspace_id) DO NOTHING
    """, (team_id, ws, name, description or "", datetime.datetime.utcnow().isoformat()))
    conn.commit()
    cur.close()


def list_teams(conn, ws):
    _set_ws(conn, ws)
    cur = conn.cursor()
    cur.execute("SELECT * FROM teams WHERE workspace_id=%s ORDER BY name", (ws,))
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


def log(conn, ws, user, query, surface, fact_ids, context_sent, team_id=None):
    _set_ws(conn, ws)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ledger(workspace_id, user_id, ts, query, surface, fact_ids, context_sent, team_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (ws, user, datetime.datetime.utcnow().isoformat(), query, surface,
          json.dumps(fact_ids), context_sent, team_id))
    conn.commit()
    cur.close()


def read_ledger(conn, ws, limit, team_id=None, offset=0):
    _set_ws(conn, ws)
    cur = conn.cursor()
    if team_id:
        cur.execute("""
            SELECT * FROM ledger WHERE workspace_id=%s AND team_id=%s
            ORDER BY id DESC LIMIT %s OFFSET %s
        """, (ws, team_id, limit, offset))
    else:
        cur.execute("""
            SELECT * FROM ledger WHERE workspace_id=%s
            ORDER BY id DESC LIMIT %s OFFSET %s
        """, (ws, limit, offset))
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Integration usage metering (COGS tracker — internal only)
# ---------------------------------------------------------------------------

def get_plan_for_account(account_id: str) -> dict:
    """Return the plan row for the account's active subscription, or the free plan."""
    if not account_id:
        return {"name": "free", "monthly_credits": 500, "max_seats": 1,
                "max_connectors": 0, "max_backfill": 500}
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT p.name, p.monthly_credits, p.max_seats,
                       p.max_connectors, p.max_backfill
                FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.account_id = %s AND s.status = 'active'
                LIMIT 1
            """, (account_id,))
            row = cur.fetchone()
            cur.close()
            if row:
                return dict(row)
            return {"name": "free", "monthly_credits": 500, "max_seats": 1,
                    "max_connectors": 0, "max_backfill": 500}
        finally:
            conn.close()
    except Exception:
        return {"name": "free", "monthly_credits": 500, "max_seats": 1,
                "max_connectors": 0, "max_backfill": 500}


def get_credits_used_this_month(account_id: str) -> int:
    """Return credits consumed this calendar month (1 credit = $0.001 spend)."""
    if not account_id:
        return 0
    import datetime as _dt
    from brain.config import CREDIT_USD_COST
    now = _dt.date.today()
    period_start = now.replace(day=1)
    if period_start.month == 12:
        period_end = period_start.replace(year=period_start.year + 1, month=1, day=1)
    else:
        period_end = period_start.replace(month=period_start.month + 1, day=1)
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT COALESCE(SUM(cost_usd), 0) AS total_cost
                FROM usage_events
                WHERE account_id = %s AND ts >= %s AND ts < %s
            """, (account_id, period_start.isoformat(), period_end.isoformat()))
            row = cur.fetchone() or {}
            cur.close()
            cost = float(row.get("total_cost") or 0.0)
            return int(cost / CREDIT_USD_COST) if CREDIT_USD_COST else 0
        finally:
            conn.close()
    except Exception:
        return 0


def get_seat_count(account_id: str) -> int:
    """Return number of active members in this account."""
    if not account_id:
        return 0
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS n FROM memberships WHERE account_id=%s AND status='active'",
                (account_id,)
            )
            row = cur.fetchone() or {}
            cur.close()
            return int(row.get("n") or 0)
        finally:
            conn.close()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# BYO-key encryption at rest
#
# Customer LLM keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
# before they touch the database, so a dump or a read-replica leak does not
# expose usable credentials. The master key comes from BRAIN_KEY_ENCRYPTION_KEY
# in the environment and is never stored alongside the data it protects.
#
# Ciphertext is stored with an "enc:v1:" prefix. Values without that prefix are
# plaintext written before encryption existed; they are still readable and are
# transparently re-encrypted the next time they are written.
# ---------------------------------------------------------------------------

_ENC_PREFIX = "enc:v1:"
_fernet = None
_fernet_checked = False


def _get_fernet():
    """Return a Fernet instance, or None when no master key is configured."""
    global _fernet, _fernet_checked
    if _fernet_checked:
        return _fernet
    _fernet_checked = True
    import os as _os
    raw = _os.getenv("BRAIN_KEY_ENCRYPTION_KEY", "").strip()
    if not raw:
        print("[thread] WARNING: BRAIN_KEY_ENCRYPTION_KEY is not set — customer LLM "
              "keys will be stored in plaintext. Generate one with: python -c "
              "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"",
              file=sys.stderr)
        return None
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(raw.encode() if isinstance(raw, str) else raw)
    except Exception as e:
        # A malformed master key must not silently downgrade to plaintext.
        raise RuntimeError(
            "BRAIN_KEY_ENCRYPTION_KEY is set but invalid — it must be a 32-byte "
            "url-safe base64 Fernet key."
        ) from e
    return _fernet


def _encrypt_secret(value: str) -> str:
    """Encrypt a secret for storage. Returns the value unchanged if no key is set."""
    if not value:
        return value
    f = _get_fernet()
    if f is None:
        return value
    return _ENC_PREFIX + f.encrypt(value.encode()).decode()


def _decrypt_secret(stored: str) -> str:
    """Decrypt a stored secret, tolerating pre-encryption plaintext rows."""
    if not stored or not stored.startswith(_ENC_PREFIX):
        return stored or ""      # legacy plaintext row
    f = _get_fernet()
    if f is None:
        # Encrypted data with no key available — fail closed rather than
        # returning ciphertext that would be sent to a provider as a credential.
        print("[thread] ERROR: stored key is encrypted but BRAIN_KEY_ENCRYPTION_KEY "
              "is not set; cannot decrypt.", file=sys.stderr)
        return ""
    try:
        return f.decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    except Exception as e:
        print(f"[thread] ERROR: failed to decrypt stored key: {e}", file=sys.stderr)
        return ""


def get_byo_key(account_id: str, provider: str) -> str:
    """Return the decrypted BYO API key for this account+provider, or '' if none."""
    if not account_id:
        return ""
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT api_key FROM workspace_llm_keys WHERE account_id=%s AND provider=%s LIMIT 1",
                (account_id, provider)
            )
            row = cur.fetchone()
            cur.close()
            return _decrypt_secret((row or {}).get("api_key", "") or "")
        finally:
            conn.close()
    except Exception:
        return ""


def upsert_byo_key(account_id: str, provider: str, api_key: str, created_by: str):
    """Encrypt and save (or replace) the BYO key for account+provider."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO workspace_llm_keys (account_id, provider, api_key, created_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, provider) DO UPDATE SET
                api_key    = EXCLUDED.api_key,
                created_by = EXCLUDED.created_by,
                created_at = now()
        """, (account_id, provider, _encrypt_secret(api_key), created_by))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def delete_byo_key(account_id: str, provider: str):
    """Remove BYO key for account+provider."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM workspace_llm_keys WHERE account_id=%s AND provider=%s",
            (account_id, provider)
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def ensure_free_subscription(account_id: str):
    """Provision a starting subscription for a newly created account if none exists.

    The tier follows the account's own type, read from the DB rather than taken
    from the caller: an 'individual' gets the 1-seat free plan, while a 'company'
    gets the team plan — on the free plan its single seat is filled by the admin,
    leaving a company structurally unable to invite the members that define it.
    """
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT type FROM accounts WHERE id = %s", (account_id,))
            row = cur.fetchone() or {}
            plan_id = "plan_team" if row.get("type") == "company" else "plan_free"
            cur.execute("""
                INSERT INTO subscriptions (account_id, plan_id, status)
                VALUES (%s, %s, 'active')
                ON CONFLICT (account_id) DO NOTHING
            """, (account_id, plan_id))
            conn.commit()
            # Confirm the row landed: this function swallows errors, and a missing
            # subscription silently degrades to the 1-seat free plan downstream.
            cur.execute(
                "SELECT plan_id FROM subscriptions WHERE account_id=%s AND status='active'",
                (account_id,))
            _got = (cur.fetchone() or {}).get("plan_id")
            if _got != plan_id:
                import sys as _s
                print(f"[Thread] subscription for {account_id}: wanted {plan_id}, "
                      f"found {_got!r}", file=_s.stderr)
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        import sys
        print(f"[Thread] ensure_free_subscription failed: {e}", file=sys.stderr)


def record_integration_usage(account_id: str, provider: str,
                              api_calls: int = 0, backfill_items: int = 0,
                              connected_accounts: int = None):
    """Upsert today's row for (account_id, provider) and increment counters.
    Runs in its own connection so it never interferes with caller's transaction."""
    if not account_id:
        return
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            if connected_accounts is not None:
                cur.execute("""
                    INSERT INTO integration_usage
                        (account_id, provider, connected_accounts, api_calls, backfill_items, period)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_DATE)
                    ON CONFLICT (account_id, provider, period) DO UPDATE SET
                        connected_accounts = %s,
                        api_calls          = integration_usage.api_calls + EXCLUDED.api_calls,
                        backfill_items     = integration_usage.backfill_items + EXCLUDED.backfill_items
                """, (account_id, provider, connected_accounts, api_calls, backfill_items,
                      connected_accounts))
            else:
                cur.execute("""
                    INSERT INTO integration_usage
                        (account_id, provider, api_calls, backfill_items, period)
                    VALUES (%s, %s, %s, %s, CURRENT_DATE)
                    ON CONFLICT (account_id, provider, period) DO UPDATE SET
                        api_calls      = integration_usage.api_calls + EXCLUDED.api_calls,
                        backfill_items = integration_usage.backfill_items + EXCLUDED.backfill_items
                """, (account_id, provider, api_calls, backfill_items))
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        import sys
        print(f"[Thread] integration_usage write failed: {e}", file=sys.stderr)
