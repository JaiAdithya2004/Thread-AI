"""
SQLite + sqlite-vec backend for Company Brain.
Active when BRAIN_DB_BACKEND is unset or "sqlite" (default).
"""
import sqlite3, json, datetime, struct, uuid
from pathlib import Path
import sqlite_vec
from brain.config import DB_FILENAME, EMBED_DIM


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect():
    conn = sqlite3.connect(DB_FILENAME)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def migrate(conn):
    conn.executescript((Path(__file__).parent / "schema.sql").read_text())
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS fact_vec "
        f"USING vec0(fact_id TEXT PRIMARY KEY, embedding FLOAT[{EMBED_DIM}])")
    for stmt in [
        "ALTER TABLE episodes ADD COLUMN team_id TEXT DEFAULT 'shared'",
        "ALTER TABLE facts ADD COLUMN team_id TEXT DEFAULT 'shared'",
        "ALTER TABLE facts ADD COLUMN visibility TEXT DEFAULT 'team'",
        "ALTER TABLE ledger ADD COLUMN team_id TEXT",
    ]:
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.commit()


# ---------------------------------------------------------------------------
# Teams / Ledger
# ---------------------------------------------------------------------------

def ensure_team(conn, ws, team_id, name, description=""):
    conn.execute(
        "INSERT OR IGNORE INTO teams(id,workspace_id,name,description,created_at)"
        " VALUES (?,?,?,?,?)",
        (team_id, ws, name, description, datetime.datetime.utcnow().isoformat()))
    conn.commit()


def list_teams(conn, ws):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM teams WHERE workspace_id=? ORDER BY name", (ws,)).fetchall()]


def log(conn, ws, user, query, surface, fact_ids, context_sent, team_id=None):
    conn.execute(
        "INSERT INTO ledger(workspace_id,user_id,ts,query,surface,fact_ids,context_sent,team_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (ws, user, datetime.datetime.utcnow().isoformat(), query, surface,
         json.dumps(fact_ids), context_sent, team_id))
    conn.commit()


def read_ledger(conn, ws, limit, team_id=None):
    if team_id:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE workspace_id=? AND team_id=? ORDER BY id DESC LIMIT ?",
            (ws, team_id, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE workspace_id=? ORDER BY id DESC LIMIT ?",
            (ws, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

def save_episode(conn, ws, ep_dict, content_hash=None) -> str:
    ep_id = ep_dict.get("id") or str(uuid.uuid4())
    ch = content_hash or ep_dict.get("content_hash")
    conn.execute(
        """INSERT OR IGNORE INTO episodes
           (id,workspace_id,source,external_id,title,body,author,url,
            occurred_at,content_hash,acl,team_id,extracted)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ep_id, ws, ep_dict.get("source"), ep_dict.get("external_id"),
         ep_dict.get("title"), ep_dict.get("body"),
         ep_dict.get("author"), ep_dict.get("url"),
         ep_dict.get("occurred_at"), ch,
         ep_dict.get("acl", "everyone"),
         ep_dict.get("team_id", "shared"),
         ep_dict.get("extracted", 0)))
    conn.commit()
    row = conn.execute(
        "SELECT id FROM episodes WHERE workspace_id=? AND content_hash=? LIMIT 1",
        (ws, ch)).fetchone()
    return row["id"] if row else ep_id


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def save_fact(conn, ws, fact_dict, embedding: list) -> str:
    fid = fact_dict.get("id") or str(uuid.uuid4())
    team_id = fact_dict.get("team_id", "shared")
    visibility = fact_dict.get("visibility", "team")

    conn.execute(
        """INSERT OR REPLACE INTO facts
           (id,workspace_id,subject,predicate,object,summary,
            episode_id,acl,confidence,introduced_at,invalidated_at,
            source_kind,team_id,visibility)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fid, ws,
         fact_dict.get("subject"), fact_dict.get("predicate"),
         fact_dict.get("object"), fact_dict.get("summary"),
         fact_dict.get("episode_id"), fact_dict.get("acl", "everyone"),
         fact_dict.get("confidence"),
         fact_dict.get("introduced_at", datetime.datetime.utcnow().isoformat()),
         fact_dict.get("invalidated_at"),
         fact_dict.get("source_kind", "llm"),
         team_id, visibility))

    if embedding:
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        conn.execute(
            "INSERT OR REPLACE INTO fact_vec(fact_id,embedding) VALUES (?,?)", (fid, blob))
        conn.execute(
            "INSERT OR REPLACE INTO fact_fts(fact_id,summary,subject,object) VALUES (?,?,?,?)",
            (fid, fact_dict.get("summary", ""),
             fact_dict.get("subject", ""), fact_dict.get("object", "")))

    conn.commit()
    return fid


def add_edge(conn, ws, src: str, dst: str, etype: str) -> None:
    conn.execute(
        "INSERT INTO fact_edges(workspace_id,src_fact,dst_fact,type) VALUES (?,?,?,?)",
        (ws, src, dst, etype))
    conn.commit()


def get_fact(conn, ws, fact_id: str):
    row = conn.execute(
        "SELECT * FROM facts WHERE id=? AND workspace_id=?", (fact_id, ws)).fetchone()
    return dict(row) if row else None


def invalidate_fact(conn, ws, fact_id: str, ts: str) -> None:
    conn.execute(
        "UPDATE facts SET invalidated_at=? WHERE id=? AND workspace_id=?",
        (ts, fact_id, ws))
    conn.commit()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_vec(conn, query_embedding: list, n: int = 30) -> list:
    if not query_embedding:
        return []
    try:
        blob = struct.pack(f"{len(query_embedding)}f", *query_embedding)
        rows = conn.execute(
            "SELECT fact_id FROM fact_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, n)).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def search_fts(conn, query: str, n: int = 30) -> list:
    if not query or not query.strip():
        return []
    # sqlite FTS5 needs clean alphanumeric tokens; strip punctuation
    clean = " ".join(t for t in query.split() if any(c.isalnum() for c in t))
    if not clean:
        return []
    try:
        rows = conn.execute(
            "SELECT fact_id FROM fact_fts WHERE fact_fts MATCH ? ORDER BY rank LIMIT ?",
            (clean, n)).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []
