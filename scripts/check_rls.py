#!/usr/bin/env python3
"""
RLS CI gate script for Company Brain / Thread.

Connects to the database and verifies that Row-Level Security is enabled
(and where required, FORCE-enabled) on all security-critical tables.

Usage:
    python scripts/check_rls.py

Exit codes:
    0  — all required RLS checks pass
    1  — one or more tables are missing RLS or FORCE RLS

Environment:
    DATABASE_URL  — Postgres connection string (required)
"""
import os
import sys

# ── Tables that MUST have RLS enabled ─────────────────────────────────────────
REQUIRED_RLS_TABLES = {
    "episodes",
    "facts",
    "fact_edges",
    "ledger",
    "teams",
    "conversations",
    "messages",
}

# ── Tables that MUST also have FORCE ROW LEVEL SECURITY ───────────────────────
# FORCE RLS prevents the table owner (postgres superuser) from bypassing policies.
REQUIRED_FORCE_RLS_TABLES = {
    "conversations",
    "messages",
}

# ── Tables to exclude from the "unlisted table has no RLS" warning ────────────
# These are infrastructure/config tables that intentionally have no RLS.
EXEMPT_TABLES = {
    "alembic_version",
    "plans",
    "accounts",
    "memberships",
    "invitations",
    "subscriptions",
    "workspace_llm_keys",
    "usage_events",
    "integration_usage",
    "dead_letter_jobs",
}


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        return 1

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        print("ERROR: psycopg2 is not installed. Run: pip install psycopg2-binary", file=sys.stderr)
        return 1

    try:
        conn = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = True
    except Exception as exc:
        print(f"ERROR: Could not connect to database: {exc}", file=sys.stderr)
        return 1

    try:
        cur = conn.cursor()

        # Query pg_class for all user tables in the public schema along with
        # their RLS flags: relrowsecurity = RLS enabled, relforcerowsecurity = FORCE RLS
        cur.execute("""
            SELECT
                c.relname                  AS table_name,
                c.relrowsecurity           AS rls_enabled,
                c.relforcerowsecurity      AS rls_forced
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'          -- ordinary tables only (not views/sequences)
            ORDER BY c.relname;
        """)
        rows = cur.fetchall()
        cur.close()
    except Exception as exc:
        print(f"ERROR: Failed to query pg_class: {exc}", file=sys.stderr)
        conn.close()
        return 1
    finally:
        conn.close()

    # Build a lookup: table_name -> {rls_enabled, rls_forced}
    table_rls: dict[str, dict] = {}
    for row in rows:
        table_rls[row["table_name"]] = {
            "rls_enabled": bool(row["rls_enabled"]),
            "rls_forced":  bool(row["rls_forced"]),
        }

    failures = 0
    print("=" * 60)
    print("RLS CI Check — Company Brain / Thread")
    print("=" * 60)

    # ── Check required RLS tables ─────────────────────────────────────────────
    print("\n[Required RLS tables]")
    for table in sorted(REQUIRED_RLS_TABLES):
        if table not in table_rls:
            status = "FAIL — table does not exist in public schema"
            failures += 1
        elif not table_rls[table]["rls_enabled"]:
            status = "FAIL — RLS is NOT enabled"
            failures += 1
        else:
            status = "PASS — RLS enabled"
        print(f"  {table:<30} {status}")

    # ── Check required FORCE RLS tables ──────────────────────────────────────
    print("\n[Required FORCE RLS tables]")
    for table in sorted(REQUIRED_FORCE_RLS_TABLES):
        if table not in table_rls:
            status = "FAIL — table does not exist"
            failures += 1
        elif not table_rls[table]["rls_enabled"]:
            status = "FAIL — RLS is NOT enabled (FORCE also missing)"
            failures += 1
        elif not table_rls[table]["rls_forced"]:
            status = "FAIL — RLS enabled but FORCE ROW LEVEL SECURITY is NOT set"
            failures += 1
        else:
            status = "PASS — FORCE RLS enabled"
        print(f"  {table:<30} {status}")

    # ── Informational: tables with no RLS that are not in the exempt list ─────
    print("\n[Other tables — informational]")
    other_tables = set(table_rls.keys()) - REQUIRED_RLS_TABLES - EXEMPT_TABLES
    if other_tables:
        for table in sorted(other_tables):
            info = table_rls[table]
            rls_note = "RLS enabled" if info["rls_enabled"] else "no RLS"
            print(f"  {table:<30} {rls_note} (not in required list)")
    else:
        print("  (none)")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if failures == 0:
        print(f"RESULT: PASS — all {len(REQUIRED_RLS_TABLES) + len(REQUIRED_FORCE_RLS_TABLES)} checks passed")
    else:
        print(f"RESULT: FAIL — {failures} check(s) failed")
    print("=" * 60)

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
