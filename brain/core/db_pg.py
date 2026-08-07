"""
Postgres + pgvector backend for Company Brain.
Uses psycopg2 (sync) so the rest of the codebase needs zero async changes.
"""
import datetime, json, uuid
from brain.config import EMBED_DIM

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect():
    import psycopg2, psycopg2.extras, os
    url = os.getenv("DATABASE_URL", "postgresql://brain:brain_local@localhost:5432/brain")
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def migrate(conn):
    """Idempotent schema setup: extensions, tables, indexes, RLS."""
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
    cur.execute(f"""
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
            embedding     vector({EMBED_DIM}),
            fts_vec       tsvector GENERATED ALWAYS AS (
                              to_tsvector('english',
                                  coalesce(summary,'') || ' ' ||
                                  coalesce(subject,'') || ' ' ||
                                  coalesce(object,''))
                          ) STORED
        );
    """)
    # HNSW index for vector similarity
    cur.execute("""
        CREATE INDEX IF NOT EXISTS facts_embedding_hnsw
            ON facts USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
    """)
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

    # workspace_llm_keys — BYO-key: encrypted at-rest (envelope encryption not yet
    # implemented; stored as plaintext in this MVP — Phase 3 adds KMS wrapping)
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

    # Grant DML to Supabase's non-bypassrls roles so SET LOCAL ROLE authenticated works
    for table in ("conversations", "messages"):
        try:
            cur.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO authenticated;"
            )
        except Exception:
            pass  # role may not exist on local Postgres installs

    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# RLS helper — set workspace for every connection before queries
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
    emb_str = "[" + ",".join(str(x) for x in embedding) + "]" if embedding else None
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO facts
            (id, workspace_id, subject, predicate, object, summary,
             episode_id, acl, confidence, introduced_at, invalidated_at,
             source_kind, team_id, visibility, embedding)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET
            subject=EXCLUDED.subject, predicate=EXCLUDED.predicate,
            object=EXCLUDED.object, summary=EXCLUDED.summary,
            confidence=EXCLUDED.confidence, embedding=EXCLUDED.embedding,
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


def search_vec(conn, query_embedding: list, n: int = 30) -> list:
    emb_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM facts
        WHERE invalidated_at IS NULL
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """, (emb_str, n))
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


def read_ledger(conn, ws, limit, team_id=None):
    _set_ws(conn, ws)
    cur = conn.cursor()
    if team_id:
        cur.execute("""
            SELECT * FROM ledger WHERE workspace_id=%s AND team_id=%s
            ORDER BY id DESC LIMIT %s
        """, (ws, team_id, limit))
    else:
        cur.execute("""
            SELECT * FROM ledger WHERE workspace_id=%s
            ORDER BY id DESC LIMIT %s
        """, (ws, limit))
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


def get_byo_key(account_id: str, provider: str) -> str:
    """Return the BYO API key for this account+provider, or '' if none."""
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
            return (row or {}).get("api_key", "") or ""
        finally:
            conn.close()
    except Exception:
        return ""


def upsert_byo_key(account_id: str, provider: str, api_key: str, created_by: str):
    """Save or replace BYO key for account+provider."""
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
        """, (account_id, provider, api_key, created_by))
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
    """Provision a free-plan subscription for a newly created account if none exists."""
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO subscriptions (account_id, plan_id, status)
                VALUES (%s, 'plan_free', 'active')
                ON CONFLICT (account_id) DO NOTHING
            """, (account_id,))
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        import sys
        print(f"[Intellark] ensure_free_subscription failed: {e}", file=sys.stderr)


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
        print(f"[Intellark] integration_usage write failed: {e}", file=sys.stderr)
