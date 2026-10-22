"""
verify_indexes.py — Assert that all required indexes exist on facts and episodes.

Usage:
    python scripts/verify_indexes.py

Exits 0 if all required indexes are present.
Exits 1 if any are missing (CI gate — run after migrations).
Requires DATABASE_URL env var or falls back to local default.
"""
import os
import sys


REQUIRED_INDEXES = {
    "facts": [
        "facts_vec_768_idx",       # HNSW vector index on fact_vec_768 (vector similarity)
        "facts_fts_gin",           # GIN full-text index (FTS retrieval)
        "facts_ws_idx",            # workspace_id lookup index
    ],
    "episodes": [
        "episodes_ws_hash",        # unique (workspace_id, content_hash) dedup index
    ],
}


def connect():
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    url = os.getenv("DATABASE_URL", "postgresql://brain:brain_local@localhost:5432/brain")
    try:
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = True
        return conn
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}", file=sys.stderr)
        print(f"  DATABASE_URL = {url}", file=sys.stderr)
        sys.exit(1)


def get_indexes_for_table(conn, table: str) -> set:
    """Return the set of index names on the given table."""
    cur = conn.cursor()
    cur.execute("""
        SELECT indexname
        FROM pg_indexes
        WHERE tablename = %s
          AND schemaname = 'public'
    """, (table,))
    rows = cur.fetchall()
    cur.close()
    return {row["indexname"] for row in rows}


def main():
    print("=" * 60)
    print("Thread — Index verification")
    print("=" * 60)

    conn = connect()
    print(f"Connected to: {os.getenv('DATABASE_URL', 'postgresql://brain:brain_local@localhost:5432/brain')}")
    print()

    missing = []
    for table, required in REQUIRED_INDEXES.items():
        existing = get_indexes_for_table(conn, table)
        print(f"Table: {table}")
        print(f"  Existing indexes: {sorted(existing)}")
        for idx in required:
            if idx in existing:
                print(f"  [OK]     {idx}")
            else:
                print(f"  [MISS]   {idx}  <-- MISSING")
                missing.append((table, idx))
        print()

    conn.close()

    print("=" * 60)
    if missing:
        print(f"FAILED: {len(missing)} required index(es) missing:")
        for table, idx in missing:
            print(f"  {table}.{idx}")
        print()
        print("Run the migration to create them:")
        print("  python -c \"from brain.core.db_pg import connect, migrate; conn=connect(); migrate(conn)\"")
        sys.exit(1)
    else:
        print("PASSED: All required indexes are present.")
        sys.exit(0)


if __name__ == "__main__":
    main()
