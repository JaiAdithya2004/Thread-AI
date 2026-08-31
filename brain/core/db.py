"""
Database access — Postgres + pgvector (Supabase or self-hosted).
All callers import from this module.
"""
from brain.core.db_pg import (  # noqa: F401
    connect,
    migrate,
    save_episode,
    save_fact,
    add_edge,
    get_fact,
    search_vec,
    search_fts,
    invalidate_fact,
    ensure_team,
    list_teams,
    log,
    read_ledger,
    get_plan_for_account,
    get_credits_used_this_month,
    get_seat_count,
    get_byo_key,
    upsert_byo_key,
    delete_byo_key,
    ensure_free_subscription,
    record_integration_usage,
)
