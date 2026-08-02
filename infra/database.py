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
    """Create the async engine with settings from environment.

    E7: Pool size and max_overflow are now configurable via
    DATABASE_POOL_SIZE and DATABASE_MAX_OVERFLOW env vars. Workers
    may need more connections than the API (e.g., 3 workers × 2
    concurrency = 6 concurrent sessions).
    """
    settings = get_settings()
    url = settings.database_url.get_secret_value()

    # SQLite (for tests) doesn't support pool_size/max_overflow
    if url.startswith("sqlite"):
        return create_async_engine(
            url,
            echo=settings.app_env == "development",
        )

    # PostgreSQL (production) — connection pooling tuned for ARM64
    return create_async_engine(
        url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
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


# Convenience: lazy session factory wrapper.
# We can't create the real factory at import time because DATABASE_URL
# might not be set yet (tests set it in conftest before importing).
# Usage: `async with async_session_factory() as session:`


class _LazySessionFactory:
    """Lazy proxy that creates the real session factory on first use."""
    _real = None

    def _get(self):
        if self._real is None:
            self._real = get_session_factory()
        return self._real

    def __call__(self):
        return self._get()()

    async def __aenter__(self):
        return await self._get().__aenter__()

    async def __aexit__(self, *args):
        return await self._get().__aexit__(*args)


async_session_factory = _LazySessionFactory()


async def get_db():
    """
    FastAPI dependency that yields an async DB session.

    Usage in route:
        @router.get("/stats")
        async def stats(session: AsyncSession = Depends(get_db)):
            ...

    Teaching note: `yield` makes this a generator dependency. FastAPI
    handles closing the session after the response is sent.
    """
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def check_db_connection() -> bool:
    """Health check: can we connect to the database?"""
    try:
        async with async_session_factory() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ── FastAPI lifecycle helpers ──

async def init_db() -> None:
    """Called on FastAPI startup — verifies DB connectivity."""
    if not await check_db_connection():
        import logging
        logging.getLogger(__name__).warning("Database not reachable on startup")
    else:
        import logging
        logging.getLogger(__name__).info("Database connected")


async def close_db() -> None:
    """Called on FastAPI shutdown — disposes the engine and connection pool."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None