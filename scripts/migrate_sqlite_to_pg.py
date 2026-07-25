"""
Migrates data from brain.db (SQLite) to Postgres.

Usage:
  python scripts/migrate_sqlite_to_pg.py

Reads from:  DB_FILENAME in brain/config.py  (default: brain.db)
Writes to:   DATABASE_URL env var (default: postgresql://brain:brain_local@localhost:5432/brain)

Safe to re-run — uses ON CONFLICT DO NOTHING / DO UPDATE for idempotency.
"""
import os, sys
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3
import psycopg2, psycopg2.extras
from brain import config

SRC = config.DB_FILENAME
DST = os.getenv("DATABASE_URL", "postgresql://brain:brain_local@localhost:5432/brain")

print(f"Source : {SRC}")
print(f"Target : {DST}")


def open_sqlite():
    if not Path(SRC).exists():
        print(f"SQLite file not found: {SRC}")
        sys.exit(1)
    conn = sqlite3.connect(SRC)
    conn.row_factory = sqlite3.Row
    return conn


def open_pg():
    conn = psycopg2.connect(DST, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    return conn


def migrate_table(sl, pg, table, insert_sql, row_fn=None):
    rows = sl.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"  {table}: 0 rows (empty)")
        return
    cur = pg.cursor()
    n = 0
    for row in rows:
        data = dict(row)
        if row_fn:
            data = row_fn(data)
        cur.execute(insert_sql, data)
        n += 1
    pg.commit()
    cur.close()
    print(f"  {table}: {n} rows copied")


def main():
    sl = open_sqlite()
    pg = open_pg()

    # Ensure schema exists in Postgres first
    from brain.core.db_pg import migrate as pg_migrate
    pg_migrate(pg)
    print("Postgres schema ready.")

    # --- teams ---
    migrate_table(sl, pg, "teams", """
        INSERT INTO teams(id, workspace_id, name, description, created_at)
        VALUES (%(id)s, %(workspace_id)s, %(name)s, %(description)s, %(created_at)s)
        ON CONFLICT (id, workspace_id) DO NOTHING
    """)

    # --- episodes ---
    migrate_table(sl, pg, "episodes", """
        INSERT INTO episodes
            (id, workspace_id, source, external_id, title, body, author, url,
             occurred_at, content_hash, acl, team_id, extracted)
        VALUES
            (%(id)s, %(workspace_id)s, %(source)s, %(external_id)s, %(title)s, %(body)s,
             %(author)s, %(url)s, %(occurred_at)s, %(content_hash)s, %(acl)s,
             %(team_id)s, %(extracted)s)
        ON CONFLICT (workspace_id, content_hash) DO NOTHING
    """, lambda r: {**r, "team_id": r.get("team_id") or "shared", "extracted": r.get("extracted") or 0})

    # --- facts (embeddings are stored as blobs in sqlite-vec, not in facts table) ---
    migrate_table(sl, pg, "facts", """
        INSERT INTO facts
            (id, workspace_id, subject, predicate, object, summary,
             episode_id, acl, confidence, introduced_at, invalidated_at,
             source_kind, team_id, visibility)
        VALUES
            (%(id)s, %(workspace_id)s, %(subject)s, %(predicate)s, %(object)s, %(summary)s,
             %(episode_id)s, %(acl)s, %(confidence)s, %(introduced_at)s, %(invalidated_at)s,
             %(source_kind)s, %(team_id)s, %(visibility)s)
        ON CONFLICT (id) DO UPDATE SET
            subject=EXCLUDED.subject, predicate=EXCLUDED.predicate,
            object=EXCLUDED.object, summary=EXCLUDED.summary,
            confidence=EXCLUDED.confidence
    """, lambda r: {
        **r,
        "team_id": r.get("team_id") or "shared",
        "visibility": r.get("visibility") or "team",
        "source_kind": r.get("source_kind") or "llm",
    })

    # --- fact_edges ---
    # fact_edges has BIGSERIAL id in pg, no id in sqlite
    rows = sl.execute("SELECT * FROM fact_edges").fetchall()
    if rows:
        cur = pg.cursor()
        for row in rows:
            cur.execute("""
                INSERT INTO fact_edges(workspace_id, src_fact, dst_fact, type)
                VALUES (%(workspace_id)s, %(src_fact)s, %(dst_fact)s, %(type)s)
            """, dict(row))
        pg.commit()
        cur.close()
        print(f"  fact_edges: {len(rows)} rows copied")
    else:
        print("  fact_edges: 0 rows (empty)")

    # --- ledger ---
    migrate_table(sl, pg, "ledger", """
        INSERT INTO ledger(workspace_id, user_id, ts, query, surface, fact_ids, context_sent, team_id)
        VALUES (%(workspace_id)s, %(user_id)s, %(ts)s, %(query)s, %(surface)s,
                %(fact_ids)s, %(context_sent)s, %(team_id)s)
    """)

    print("\nMigration complete.")
    print("NOTE: Vector embeddings are not migrated — run 'brain backfill' to re-embed facts.")

    sl.close()
    pg.close()


if __name__ == "__main__":
    main()
