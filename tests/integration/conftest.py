"""
tests/integration/conftest.py — Fixtures for integration tests

Integration tests connect to a real PostgreSQL instance (not the in-memory
SQLite used by unit tests). They exercise PostgreSQL-specific features like
FOR UPDATE SKIP LOCKED and ON CONFLICT DO NOTHING.

EVENT LOOP NOTE
---------------
pytest-asyncio creates a fresh event loop per test function. The
module-level async engine in ``infra.database`` caches asyncpg connections
bound to the first event loop that used them. When a later test runs on a
*new* event loop, ``pool_pre_ping=True`` tries to ping a stale connection
and asyncpg raises::

    RuntimeError: ... got Future ... attached to a different loop

We cannot ``await engine.dispose()`` on the old engine because its event
loop is already closed by the time the next test starts. Instead, the
``reset_engine`` autouse fixture (synchronous) simply drops the module-level
references so that ``get_session_factory()`` lazily builds a fresh engine
bound to the current test's event loop. The old engine is left for garbage
collection; its connections die with the closed loop. This only affects the
test-time module cache — production behaviour is unchanged.
"""

import pytest

import infra.database
from infra.database import Base

# Import all models so Base.metadata knows about every table.
import domain.models  # noqa: F401


@pytest.fixture(autouse=True)
def reset_engine():
    """Drop module-level engine/factory cache so each test gets a fresh engine.

    Must run *before* the test's async event loop is used for DB access,
    so a sync autouse fixture is correct here. We clear the references;
    the old engine (bound to a now-dead loop) is abandoned for GC.
    """
    infra.database._engine = None
    infra.database._async_session_factory = None
    yield
    # Clear again after the test so the next test doesn't reuse a stale engine.
    infra.database._engine = None
    infra.database._async_session_factory = None


@pytest.fixture(autouse=True)
def create_tables():
    """Create all tables in the test database before each test.

    Uses a SEPARATE engine (not the module-level one) so we don't
    pollute the event-loop-bound engine that the test will use.
    The test's own event loop builds its engine via get_engine()
    after reset_engine cleared the cache.
    """
    import asyncio
    import os
    from sqlalchemy.ext.asyncio import create_async_engine

    db_url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://localhost/sms_first_test"
    )

    async def _create():
        engine = create_async_engine(db_url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    async def _drop():
        engine = create_async_engine(db_url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()

    asyncio.run(_create())
    yield
    asyncio.run(_drop())