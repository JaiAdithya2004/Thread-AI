"""
Postgres + pgvector backend for Company Brain.
Drop-in replacement for db.py — identical public signatures.

Activated when BRAIN_DB_BACKEND=postgres (or DATABASE_URL is set).
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

    # Row-Level Security — defense in depth, workspace isolation
    for table in ("episodes", "facts", "fact_edges", "ledger", "teams"):
        cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        cur.execute(f"""
            DROP POLICY IF EXISTS ws_isolation ON {table};
        """)
        cur.execute(f"""
            CREATE POLICY ws_isolation ON {table}
                USING (workspace_id = current_setting('brain.workspace_id', true));
        """)

    conn.commit()
    cur.close()


# ---------------------------------------------------------------------------
# RLS helper — set workspace for every connection before queries
# ---------------------------------------------------------------------------

def _set_ws(conn, ws: str):
    cur = conn.cursor()
    cur.execute("SELECT set_config('brain.workspace_id', %s, false)", (ws,))
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
