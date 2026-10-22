"""
Database access — Postgres + pgvector (Supabase or self-hosted).
All callers import from this module.
"""
# Postgres is the only backend. Callers branch on this name to skip billing and
# plan logic when a backend without those tables is in use; removing it left
# every `from brain.core.db import BACKEND` raising ImportError, which broke
# plan limits, usage reporting, backfill and the dead-letter endpoints.
BACKEND = "postgres"

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
    get_team_goals,
    set_team_mission,
    upsert_team_kpi,
    delete_team_kpi,
    log,
    read_ledger,
    get_plan_for_account,
    get_credits_used_this_month,
    get_seat_count,
    get_byo_key,
    get_byo_keys,
    upsert_byo_key,
    delete_byo_key,
    ensure_free_subscription,
    record_integration_usage,
)
