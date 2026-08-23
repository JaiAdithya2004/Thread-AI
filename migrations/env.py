"""
Alembic environment configuration for Company Brain / Thread.

Reads DATABASE_URL from the environment and uses it for both
online migrations (direct connection) and offline SQL generation.

Usage:
    alembic upgrade head        # apply all pending migrations
    alembic downgrade -1        # roll back one migration
    alembic revision -m "desc"  # create a new (empty) migration
    alembic revision --autogenerate -m "desc"  # auto-detect schema diff
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Add project root to sys.path so brain.* imports work if needed
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Alembic Config object providing access to alembic.ini values
config = context.config

# Override sqlalchemy.url from environment variable (required in production)
database_url = os.environ.get("DATABASE_URL")
if not database_url:
    # Fallback to alembic.ini value for local dev
    database_url = config.get_main_option("sqlalchemy.url")

if not database_url:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set and no sqlalchemy.url "
        "found in alembic.ini. Set DATABASE_URL before running Alembic."
    )

config.set_main_option("sqlalchemy.url", database_url)

# Set up Python logging from alembic.ini if the file is present
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# We do NOT use SQLAlchemy ORM metadata here — migrations are raw SQL
# (brain uses psycopg2 directly, not SQLAlchemy ORM).
target_metadata = None


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.
    Generates SQL to stdout without connecting to the database.
    Useful for dry-run review before applying changes.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Wrap each migration in BEGIN/COMMIT for transactional DDL
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode (connects directly to the database).
    Each migration runs inside a transaction; DDL errors roll back cleanly.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # no pooling — we close after migration
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Wrap each migration revision in its own transaction
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
