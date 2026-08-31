"""
Database access — Postgres + pgvector (Supabase or self-hosted).
All callers import from this module.
"""
from brain.core.db_pg import connect, migrate  # noqa: F401
