"""
infra/rate_limiter.py — Redis-backed rate limiting for admin API endpoints
════════════════════════════════════════════════════════════════════════

PURPOSE
-------
Protects admin endpoints from brute-force token attacks and abusive
polling by **business owners** (or anyone hitting the admin dashboard).
Uses a sliding-window counter via Redis INCR + EXPIRE.

The rate limiter is *provider-agnostic* — it knows nothing about SMS
gateways, payment APIs, or any specific integration. It only counts
requests per key per time window. This is why the logic ports unchanged
from the tuition agent: a Redis counter is a Redis counter whether you
are rate-limiting school administrators or business owners.

KEY DESIGN DECISIONS
--------------------
  1. **Redis INCR + EXPIRE (not a Lua script)** — simplicity wins.
     The first request in a window sets the TTL; subsequent requests
     only INCR. The window is *fixed* from the first request, not truly
     sliding, which is acceptable for admin-dashboard protection.
  2. **State in Redis, not in-memory** — every Celery worker and every
     FastAPI process shares the same counter. A worker that restarts
     does not reset the client's rate-limit budget.
  3. **Stateless singleton** — ``RateLimiter`` holds no instance state;
     all state lives in Redis keys. This makes the module-level
     ``rate_limiter`` singleton safe to share across coroutines.
  4. **FastAPI dependency** — ``rate_limit_dependency`` wraps the limiter
     so any route can add ``Depends(rate_limit_dependency)`` and get
     automatic 429 responses with a ``Retry-After`` header.

ADAPTATION FROM THE TUITION AGENT
--------------------------------
  - ``"school administrators"`` → ``"business owners"`` in docstrings.
  - Import changed from ``from infra.redis_pool import redis_client``
    to ``from infra.redis_pool import get_redis_client`` because the
    SMS-first-agent's ``redis_pool.py`` exposes a lazy accessor
    (``get_redis_client()``) instead of a module-level ``redis_client``
    variable. The ``get_redis_client()`` call returns the cached
    singleton after first creation, so the runtime behaviour is
    identical.

TEACHING NOTES
--------------
  - ``decode_responses=True`` on the Redis client (set in
    ``redis_pool.py``) means ``INCR`` returns a Python ``str``, so we
    cast to ``int`` explicitly: ``int(await redis.incr(...))``.
  - The FastAPI dependency reads the client IP from
    ``Request.client.host``. Behind a reverse proxy, use
    ``X-Forwarded-For`` (configure via ``uvicorn --proxy-headers`` or
    a middleware to trust the header).
  - ``HTTPException`` with status 429 and a ``Retry-After`` header is
    the HTTP-standard way to signal rate limiting. API clients can read
    the header to implement exponential backoff.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - ``infra/redis_pool.py`` — provides the Redis client (``get_redis_client``).
  - ``api/routes/admin.py`` (or equivalent) — imports
    ``rate_limit_dependency`` and adds it to route ``dependencies``.
  - ``infra/circuit_breaker.py`` — a *different* resilience pattern
    (fail-fast vs. rate-limit). Both use Redis but for different purposes.

════════════════════════════════════════════════════════════════════════
"""

from fastapi import HTTPException, Request, status

# ── Adaptation note ──────────────────────────────────────────────────
# The tuition agent used a module-level `redis_client` singleton. The
# SMS-first-agent's redis_pool.py uses a lazy factory (`get_redis_client`)
# that creates the Redis connection on first access and caches it. We
# call `get_redis_client()` at the top of each async method to obtain the
# cached client. After the first call this is just a dict lookup — there
# is no connection overhead.
from infra.redis_pool import get_redis_client


class RateLimiter:
    """
    Redis-backed fixed-window rate limiter.

    The limiter is *stateless* — all counter state lives in Redis keys
    of the form ``ratelimit:<key>``. This means the same limiter
    instance (or the module-level ``rate_limiter`` singleton) can be
    shared across all coroutines, Celery workers, and FastAPI processes
    safely.

    Usage::

        limiter = RateLimiter()
        allowed, retry_after = await limiter.check_rate_limit(
            key="admin:203.0.113.5",
            limit=60,
            window_seconds=60,
        )
        if not allowed:
            raise HTTPException(
                429,
                "Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
    """

    async def check_rate_limit(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        """
        Check whether a request is allowed under the rate limit.

        Uses Redis **INCR + EXPIRE**:

          1. **INCR** the counter key (atomic, starts at 1 if the key
             does not exist).
          2. If this is the first request (count == 1), set **EXPIRE**
             so the key auto-deletes after the window elapses.
          3. If count > limit, reject and compute retry-after from the
             remaining TTL.

        The window is *fixed* — it starts when the first request arrives
        and ends when the TTL expires. This is simpler than a true
        sliding-window (which needs a sorted set or Lua script) and is
        perfectly adequate for protecting admin dashboards.

        Args:
            key: Unique identifier for the rate-limit bucket
                 (e.g., ``"admin:ip:203.0.113.5"``).
            limit: Maximum requests allowed in the window.
            window_seconds: Size of the fixed window in seconds.

        Returns:
            A tuple of ``(allowed, retry_after_seconds)``.
            When *allowed* is ``False``, *retry_after_seconds* indicates
            how long the client should wait before retrying.
        """
        # Obtain the lazily-initialised Redis client. After the first
        # call in this process, this is just a cached lookup — no new
        # connection is opened.
        redis = get_redis_client()

        # All rate-limit keys are namespaced under "ratelimit:" so they
        # don't collide with circuit-breaker keys ("circuit:") or
        # idempotency keys used elsewhere.
        redis_key = f"ratelimit:{key}"

        # ── Step 1: Atomically increment the counter ──
        # INCR is atomic in Redis — even with concurrent workers, each
        # gets a unique count. decode_responses=True means the return
        # is a str, so we cast to int.
        count = int(await redis.incr(redis_key))

        # ── Step 2: Set TTL only on the first request ──
        # If we set EXPIRE on every request, the window would keep
        # extending (effectively a sliding window). By setting it only
        # when count == 1, the window is fixed from the first hit.
        if count == 1:
            await redis.expire(redis_key, window_seconds)

        # ── Step 3: Check against the limit ──
        if count > limit:
            # TTL tells the client exactly how long until the window
            # resets. If TTL is -2 (key expired) or -1 (no TTL), fall
            # back to the full window as a safe default.
            ttl = await redis.ttl(redis_key)
            retry_after = ttl if ttl > 0 else window_seconds
            return (False, retry_after)

        return (True, 0)


# ── Module-level singleton ────────────────────────────────────────────
# Safe to share because RateLimiter holds no mutable instance state —
# every call goes to Redis. Celery workers, FastAPI processes, and test
# code all use this same instance.
rate_limiter = RateLimiter()


# ── FastAPI Dependency ────────────────────────────────────────────────


async def rate_limit_dependency(request: Request) -> None:
    """
    FastAPI dependency that limits admin endpoints to **60 requests per
    minute** per client IP.

    This protects business owners' admin dashboard endpoints from
    brute-force token attacks and abusive polling. Any route that
    needs rate limiting simply adds this to its ``dependencies`` list.

    Usage in a router::

        from fastapi import Depends
        from infra.rate_limiter import rate_limit_dependency

        @router.get(
            "/dashboard/stats",
            dependencies=[
                Depends(verify_admin_token),      # auth first
                Depends(rate_limit_dependency),     # then rate-limit
            ],
        )
        async def dashboard_stats(...): ...

    The order matters: authentication should run *before* rate-limiting
    so that unauthenticated requests are rejected before they consume
    rate-limit budget. (Though in practice, the dependency order also
    means unauthenticated requests still get rate-limited if auth
    passes.)

    Raises:
        HTTPException: **429 Too Many Requests** with a ``Retry-After``
            header (in seconds) when the limit is exceeded.
    """
    # client.host is the peer IP. Behind a reverse proxy (nginx, Caddy),
    # ensure Uvicorn is started with --proxy-headers so this reflects the
    # real client IP from X-Forwarded-For, not the proxy's IP.
    client_ip = request.client.host if request.client else "unknown"

    # The key is namespaced by "admin:ip:" so we can later add other
    # rate-limit buckets (e.g., "webhook:ip:" for inbound webhook
    # endpoints) without collision.
    allowed, retry_after = await rate_limiter.check_rate_limit(
        key=f"admin:ip:{client_ip}",
        limit=60,            # 60 requests per minute per IP
        window_seconds=60,
    )

    if not allowed:
        # 429 is the HTTP standard for rate limiting. The Retry-After
        # header tells well-behaved clients when to try again.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )