"""
explain_queries.py — EXPLAIN ANALYZE the three hot queries on the facts table.

Usage:
    python scripts/db/explain_queries.py

Exits 1 if any hot query uses a sequential scan on facts (CI gate).
Requires DATABASE_URL env var or falls back to local default.
"""
import json
import os
import sys


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


def explain(conn, label: str, sql: str, params: tuple) -> dict:
    """Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) and return the plan dict."""
    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"
    cur = conn.cursor()
    cur.execute(explain_sql, params)
    row = cur.fetchone()
    cur.close()
    # psycopg2 returns the JSON plan as a Python object (via RealDictCursor)
    plan = row[list(row.keys())[0]]
    if isinstance(plan, str):
        plan = json.loads(plan)
    return plan[0] if isinstance(plan, list) else plan


def has_seq_scan_on_facts(plan_node: dict) -> bool:
    """Recursively check if any node is a sequential scan on the facts table."""
    node_type = plan_node.get("Node Type", "")
    relation = plan_node.get("Relation Name", "")
    if node_type == "Seq Scan" and relation == "facts":
        return True
    for child in plan_node.get("Plans", []):
        if has_seq_scan_on_facts(child):
            return True
    return False


def get_execution_time_ms(plan: dict) -> float:
    return plan.get("Plan", {}).get("Actual Total Time", 0.0)


def get_root_node(plan: dict) -> dict:
    return plan.get("Plan", {})


def print_plan_summary(label: str, plan: dict):
    root = get_root_node(plan)
    exec_ms = get_execution_time_ms(plan)
    node_type = root.get("Node Type", "unknown")
    seq = has_seq_scan_on_facts(root)
    status = "SEQ SCAN (BAD)" if seq else "Index used (OK)"
    print(f"  [{label}]")
    print(f"    Root node:      {node_type}")
    print(f"    Execution time: {exec_ms:.2f} ms")
    print(f"    Index status:   {status}")
    if seq:
        print(f"    WARNING: Sequential scan on facts — add/rebuild the required index!")


def main():
    print("=" * 60)
    print("Thread — EXPLAIN ANALYZE hot queries")
    print("=" * 60)

    conn = connect()
    print(f"Connected to: {os.getenv('DATABASE_URL', 'postgresql://brain:brain_local@localhost:5432/brain')}")
    print()

    # Determine vector dimension from an actual embedding or use config
    embed_dim = int(os.getenv("EMBED_DIM", "384"))
    dummy_embedding = "[" + ",".join(["0.1"] * embed_dim) + "]"
    workspace_id = os.getenv("TEST_WORKSPACE_ID", "test-workspace")
    fts_query = "company decision policy"

    queries = [
        (
            "1. Vector similarity (HNSW)",
            """
            SELECT id, embedding <=> %s::vector AS dist
            FROM facts
            WHERE workspace_id = %s
              AND invalidated_at IS NULL
            ORDER BY embedding <=> %s::vector
            LIMIT 20
            """,
            (dummy_embedding, workspace_id, dummy_embedding),
        ),
        (
            "2. Full-text search (GIN)",
            """
            SELECT id, ts_rank(fts_vec, to_tsquery('english', %s)) AS rank
            FROM facts
            WHERE workspace_id = %s
              AND invalidated_at IS NULL
              AND fts_vec @@ to_tsquery('english', %s)
            ORDER BY rank DESC
            LIMIT 20
            """,
            (fts_query, workspace_id, fts_query),
        ),
        (
            "3. Combined RRF candidate fetch",
            """
            SELECT id, summary, confidence, invalidated_at, team_id, visibility, acl
            FROM facts
            WHERE workspace_id = %s
              AND invalidated_at IS NULL
            ORDER BY introduced_at DESC
            LIMIT 30
            """,
            (workspace_id,),
        ),
    ]

    any_seq_scan = False
    for label, sql, params in queries:
        print(f"Running: {label}")
        try:
            plan = explain(conn, label, sql, params)
            print_plan_summary(label, plan)
            root = get_root_node(plan)
            if has_seq_scan_on_facts(root):
                any_seq_scan = True
        except Exception as e:
            print(f"  ERROR running EXPLAIN: {e}", file=sys.stderr)
            any_seq_scan = True
        print()

    conn.close()

    print("=" * 60)
    if any_seq_scan:
        print("FAILED: One or more hot queries are using a sequential scan on facts.")
        print("  Ensure migration has been run: python -m brain.core.db_pg")
        print("  Then run: python scripts/db/verify_indexes.py")
        sys.exit(1)
    else:
        print("PASSED: All hot queries are using indexes.")
        sys.exit(0)


if __name__ == "__main__":
    main()
