import sqlite3, json, datetime
from pathlib import Path
import sqlite_vec
from brain.config import DB_FILENAME, EMBED_DIM

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
    # Add columns to existing databases idempotently
    for stmt in [
        "ALTER TABLE episodes ADD COLUMN team_id TEXT DEFAULT 'shared'",
        "ALTER TABLE facts ADD COLUMN team_id TEXT DEFAULT 'shared'",
        "ALTER TABLE facts ADD COLUMN visibility TEXT DEFAULT 'team'",
        "ALTER TABLE ledger ADD COLUMN team_id TEXT",
    ]:
        try:
            conn.execute(stmt)
        except Exception:
            pass  # column already exists
    conn.commit()

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
