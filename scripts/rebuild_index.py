"""
Phase 4A — Build HNSW index on fact_vec_768.

Run AFTER reembed_gemini.py has fully populated all fact_vec_768 vectors.
Aborts with exit code 1 if any NULLs remain.

Usage:
    python scripts/rebuild_index.py
"""
import os
import sys


def main():
    import psycopg2

    url = os.getenv("DATABASE_URL", "postgresql://brain:brain_local@localhost:5432/brain")

    # 1. Connect in normal mode to run the null-check query.
    conn = psycopg2.connect(url)
    conn.autocommit = False
    cur = conn.cursor()

    print("[rebuild_index] Checking for un-embedded facts ...")
    cur.execute("SELECT COUNT(*) FROM facts WHERE fact_vec_768 IS NULL")
    row = cur.fetchone()
    null_count = row[0] if row else 0
    cur.close()
    conn.close()

    if null_count > 0:
        print(
            f"[rebuild_index] ABORT: {null_count} fact(s) still have NULL fact_vec_768. "
            "Run reembed_gemini.py first — N facts still need embedding",
            file=sys.stderr,
        )
        sys.exit(1)

    print("[rebuild_index] All facts have fact_vec_768 populated. Building HNSW index ...")

    # 2. Reconnect with autocommit=True — CREATE INDEX CONCURRENTLY cannot run
    #    inside a transaction block.
    conn2 = psycopg2.connect(url)
    conn2.autocommit = True

    try:
        cur2 = conn2.cursor()
        cur2.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS facts_vec_768_idx "
            "ON facts USING hnsw (fact_vec_768 vector_cosine_ops) "
            "WITH (m=16, ef_construction=64)"
        )
        cur2.close()
    finally:
        conn2.close()

    print("[rebuild_index] Done. Index facts_vec_768_idx created successfully.")


if __name__ == "__main__":
    main()
