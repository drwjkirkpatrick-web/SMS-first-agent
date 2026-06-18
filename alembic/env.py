"""
alembic/env.py — Alembic migration environment
═══════════════════════════════════════════════════

Alembic needs to know about our Base class (to discover tables) and
our database URL (to connect for migrations).

Teaching notes:
  - `target_metadata` tells Alembic what tables to compare against
    the database when running `alembic revision --autogenerate`.
  - We import ALL models here so Alembic sees them. If you add a new
    model and forget to import it here, `autogenerate` won't detect it.
  - `run_migrations_offline()` generates SQL without a DB connection
    (useful for CI pipelines that just check the SQL).
═══════════════════════════════════════════════════
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from infra.database import Base
from infra.settings import get_settings

# Import all models so Alembic discovers them
from domain import models  # noqa: F401 — side effect: registers tables

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Set the database URL from our settings
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url.get_secret_value())


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to DB and execute."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()