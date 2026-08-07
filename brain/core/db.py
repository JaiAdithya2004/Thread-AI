"""
Database access — Postgres + pgvector (Supabase or self-hosted).
All callers import from this module.
"""
from brain.core.db_pg import (
    connect, migrate,
    save_episode, save_fact, add_edge, get_fact,
    search_vec, search_fts, invalidate_fact,
    ensure_team, list_teams, log, read_ledger,
)

BACKEND = "postgres"
