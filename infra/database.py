"""
infra/database.py — SQLAlchemy async engine and session factory
═══════════════════════════════════════════════════

This module sets up the PostgreSQL connection for the entire app.
Every module that needs DB access imports `async_session_factory`.

Design decisions:
  1. We use async SQLAlchemy (asyncpg driver) so the FastAPI server
     and Celery workers can handle concurrent DB operations without
     blocking the event loop.
  2. `pool_size=5` is tuned for ARM64 (Raspberry Pi has limited RAM).
     Each Celery worker process gets its own connection pool.
  3. `pool_pre_ping=True` sends a SELECT 1 before reusing a connection
     — critical for Kenya where network drops can leave stale connections.
  4. The `Base` class is the declarative base for all ORM models.
     Alembic imports it to discover tables for migrations.

Teaching notes:
  - `create_async_engine` returns an engine, not a connection.
    Connections are created per-session via `async_session_factory`.
  - `expire_on_commit=False` means objects remain usable after commit.
    Without this, accessing `obj.field` after commit triggers a lazy
    load that fails in async context.
  - On the Raspberry Pi, keep pool_size low (5 is plenty for a small
    business with < 1000 customers).
═══════════════════════════════════════════════════
"""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from infra.settings import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models. Alembic discovers tables from here."""
    pass


def _build_engine():
    """Create the async engine with settings from environment."""
    settings = get_settings()
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,       # critical for unreliable networks
        pool_recycle=300,          # recycle connections every 5 min
        echo=settings.app_env == "development",
    )


# Module-level engine (one per process)
_engine = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    """Lazily create the engine (avoids connecting at import time)."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory, creating it on first call."""
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


# Convenience alias used throughout the codebase
# Usage: `async with async_session_factory() as session:`
async_session_factory = get_session_factory()


async def check_db_connection() -> bool:
    """Health check: can we connect to the database?"""
    try:
        async with async_session_factory() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False