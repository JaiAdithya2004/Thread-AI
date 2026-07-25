"""
Storage router: delegates to db_sqlite or db_pg based on BRAIN_DB_BACKEND env var.

  BRAIN_DB_BACKEND=sqlite   (default) — local SQLite + sqlite-vec
  BRAIN_DB_BACKEND=postgres  — Postgres 16 + pgvector (requires DATABASE_URL)

All callers use this module; they never import db_sqlite or db_pg directly.
"""
import os

_BACKEND = os.getenv("BRAIN_DB_BACKEND", "sqlite").lower()

if _BACKEND in ("postgres", "pg", "postgresql"):
    from brain.core.db_pg import (
        connect, migrate,
        save_episode, save_fact, add_edge, get_fact,
        search_vec, search_fts, invalidate_fact,
        ensure_team, list_teams, log, read_ledger,
    )
    BACKEND = "postgres"
else:
    from brain.core.db_sqlite import (
        connect, migrate,
        save_episode, save_fact, add_edge, get_fact,
        search_vec, search_fts, invalidate_fact,
        ensure_team, list_teams, log, read_ledger,
    )
    BACKEND = "sqlite"
