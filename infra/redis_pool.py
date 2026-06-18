"""
infra/redis_pool.py — Redis connection pool for Celery broker + cache
═══════════════════════════════════════════════════

Redis serves two purposes:
  1. Celery message broker (task queue)
  2. Application cache (rate limits, idempotency keys, connectivity status)

For a Raspberry Pi deployment, Redis runs locally alongside the app.
Memory usage is minimal (~10 MB for a small business).

Teaching notes:
  - `decode_responses=True` means Redis returns strings, not bytes.
    This simplifies our Python code (no manual decode).
  - `ConnectionPool` reuses connections — don't create a new client
    per request. The module-level `redis_client` is shared.
  - On the Pi, Redis doesn't need persistence (it's a cache/broker).
    Set `save ""` in redis.conf to disable disk writes (reduces SD
    card wear).
═══════════════════════════════════════════════════
"""

import redis.asyncio as redis
from infra.settings import get_settings

# Module-level pool — lazily created on first access
_redis_client: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    """Return the Redis client, creating it on first call."""
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = redis.from_url(
            settings.redis_url.get_secret_value(),
            decode_responses=True,
            max_connections=10,       # low for Pi
            socket_timeout=5,         # fail fast if Redis is down
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
    return _redis_client


# ── FastAPI lifecycle helpers ──

async def ping_redis() -> None:
    """Called on FastAPI startup — verifies Redis connectivity."""
    if not await check_redis_connection():
        import logging
        logging.getLogger(__name__).warning("Redis not reachable on startup")
    else:
        import logging
        logging.getLogger(__name__).info("Redis connected")


async def close_redis() -> None:
    """Called on FastAPI shutdown — closes Redis connection pool."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


async def check_redis_connection() -> bool:
    """Health check: can we connect to Redis?"""
    try:
        client = get_redis_client()
        await client.ping()
        return True
    except Exception:
        return False