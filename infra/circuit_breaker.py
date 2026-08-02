"""
infra/circuit_breaker.py — Circuit breaker for external API calls
════════════════════════════════════════════════════════════════════════

PURPOSE
-------
Wraps calls to external services (Africa's Talking SMS gateway, M-Pesa
Daraja payment API) with a **circuit-breaker** pattern so that a flapping
dependency does not exhaust Celery worker threads or burn API quota on
requests that are doomed to fail.

In the Kenya small-business context, the two most common external
dependencies are:
  - **Africa's Talking** — primary SMS gateway for sending/receiving SMS.
  - **M-Pesa (Daraja API)** — Safaricom's payment API for Lipa na M-Pesa
    online checkout and C2B callbacks.

Both can experience outages, rate-limit responses, or network issues.
The circuit breaker detects sustained failures and "opens" the circuit,
short-circuiting all subsequent calls until the dependency recovers.

STATES
------
  CLOSED    — Requests flow normally. Failures are counted.
  OPEN      — All requests are rejected immediately. After a cooldown
              period, the breaker transitions to HALF_OPEN.
  HALF_OPEN — A single "test" request is allowed. If it succeeds, the
              breaker closes. If it fails, it re-opens.

KEY DESIGN DECISIONS
--------------------
  1. **State in Redis (not in-memory)** — all Celery workers share the
     same circuit state. A worker that sees OPEN will short-circuit
     without even trying the API. This is critical because Celery may
     run 4+ workers on the Pi, each with independent memory.
  2. **Separate Redis keys** for failure count (``circuit:<key>:failures``),
     state (``circuit:<key>:state``), and open-timestamp
     (``circuit:<key>:opened_at``). A Lua script would be more atomic,
     but for the expected throughput (Africa's Talking SMS sends, M-Pesa
     payment requests) the small race window is acceptable — at worst,
     one extra test request slips through during a transition.
  3. **Per-integration keys** — each external service gets its own
     circuit (``"africas_talking"``, ``"mpesa"``, etc.). A Twilio
     outage does not trip the M-Pesa circuit and vice versa.
  4. **Default thresholds: 5 consecutive failures → OPEN for 60s** —
     these can be tuned per integration key by passing custom parameters
     to the ``CircuitBreaker`` constructor.

ADAPTATION FROM THE TUITION AGENT
--------------------------------
  - ``"Twilio"`` → ``"Africa's Talking"`` in comments and examples.
  - Added ``"mpesa"`` as another example integration key alongside
    ``"twilio"`` (which is kept as a fallback SMS gateway).
  - Import changed from ``from infra.redis_pool import redis_client``
    to ``from infra.redis_pool import get_redis_client`` (same
    adaptation as rate_limiter.py — see that file's docstring for
    details).
  - The ``CircuitBreaker`` class, ``CircuitState`` enum, and
    ``circuit_breaker`` singleton are otherwise unchanged.

TEACHING NOTES
--------------
  - The circuit-breaker pattern was popularised by Michael Nygard's
    book *Release It!* (2007) and is now a standard microservice
    resilience pattern. It is the "electrical fuse" of distributed
    systems: when a downstream service is down, stop trying to call it
    so your workers stay free for other work.
  - The ``HALF_OPEN`` state is the clever part: instead of waiting a
    fixed time and blindly closing the circuit (which could cause a
    thundering herd), only *one* test request is allowed. If that
    request succeeds, the circuit closes and traffic resumes. If it
    fails, the circuit re-opens for another cooldown.
  - ``time.time()`` returns a Unix timestamp (seconds since epoch). We
    store it in Redis as an integer string and compare against the
    current time to decide if the cooldown has elapsed.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - ``infra/redis_pool.py`` — provides the Redis client.
  - ``adapters/africas_talking.py`` — wraps SMS sends in
    ``circuit_breaker.can_execute("africas_talking")``.
  - ``adapters/mpesa_adapter.py`` — wraps payment API calls in
    ``circuit_breaker.can_execute("mpesa")``.
  - ``infra/rate_limiter.py`` — a *different* resilience pattern
    (throttle input vs. fail-fast on output). Both use Redis.
  - ``infra/connectivity_watcher.py`` — monitors API health and can
    work alongside the circuit breaker for proactive monitoring.

════════════════════════════════════════════════════════════════════════
"""

from enum import Enum

# ── Adaptation note ──────────────────────────────────────────────────
# Same as rate_limiter.py: the SMS-first-agent's redis_pool exposes a
# lazy factory. We call get_redis_client() in each method to get the
# cached singleton.
from infra.redis_pool import get_redis_client


class CircuitState(str, Enum):
    """
    Circuit breaker states.

    Stored as string values in Redis so they are human-readable when
    inspecting keys with ``redis-cli``:

        $ redis-cli GET circuit:africas_talking:state
        "open"

    Inherits from ``str`` so comparisons like
    ``state == CircuitState.OPEN`` work against both the enum and the
    raw Redis string value.
    """
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ── Default tuning parameters ────────────────────────────────────────
# 5 consecutive failures → open the circuit for 60 seconds.
# These are conservative defaults; adjust per integration if needed.
DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_COOLDOWN_SECONDS = 60


class CircuitBreaker:
    """
    Redis-backed circuit breaker for external API calls.

    Each *key* (e.g., ``"africas_talking"``, ``"mpesa"``, ``"twilio"``)
    maintains an independent circuit. All state lives in Redis so it is
    shared across Celery workers and FastAPI processes.

    Usage::

        breaker = CircuitBreaker()

        # Check before calling the API
        if await breaker.can_execute("africas_talking"):
            try:
                await africas_talking_client.send(...)
                await breaker.record_success("africas_talking")
            except Exception:
                await breaker.record_failure("africas_talking")
        else:
            # Circuit is open — skip the call, queue for retry later
            logger.warning("Africa's Talking circuit is open — deferring")

        # Same pattern for M-Pesa payment API:
        if await breaker.can_execute("mpesa"):
            try:
                await mpesa_adapter.stk_push(...)
                await breaker.record_success("mpesa")
            except Exception:
                await breaker.record_failure("mpesa")
    """

    def __init__(
        self,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        """
        Configure the breaker thresholds.

        Args:
            failure_threshold: Number of consecutive failures before
                the circuit opens. Default: 5.
            cooldown_seconds: How long the circuit stays open before
                transitioning to HALF_OPEN. Default: 60.

        These are shared across all keys — if you need per-key tuning,
        create multiple ``CircuitBreaker`` instances or extend the
        class to accept per-key overrides.
        """
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

    # ── Redis key helpers ────────────────────────────────────────────
    # Each integration gets three keys under the "circuit:" namespace:
    #   circuit:<key>:state     → "closed" | "open" | "half_open"
    #   circuit:<key>:failures  → integer failure count
    #   circuit:<key>:opened_at → Unix timestamp when circuit opened

    @staticmethod
    def _state_key(key: str) -> str:
        """Redis key for the circuit state."""
        return f"circuit:{key}:state"

    @staticmethod
    def _failures_key(key: str) -> str:
        """Redis key for the consecutive failure count."""
        return f"circuit:{key}:failures"

    @staticmethod
    def _opened_key(key: str) -> str:
        """Redis key for the timestamp when the circuit opened."""
        return f"circuit:{key}:opened_at"

    # ── Public API ────────────────────────────────────────────────────

    async def can_execute(self, key: str) -> bool:
        """
        Check whether a request to *key* may proceed.

        Returns ``True`` when the circuit is **CLOSED** or when it is
        **HALF_OPEN** (allowing a single test request). Returns ``False``
        when the circuit is **OPEN** and the cooldown has not elapsed.

        Side effects:
          - If the OPEN cooldown has expired, transitions the circuit to
            HALF_OPEN and returns ``True`` (the caller becomes the test
            request).

        This is the first call in the circuit-breaker dance:

            if await breaker.can_execute("africas_talking"):
                ...  # make the API call
            else:
                ...  # skip / queue for later
        """
        redis = get_redis_client()

        # Read the current circuit state from Redis.
        # No state key → circuit has never tripped → CLOSED (implicit).
        state = await redis.get(self._state_key(key))

        # ── CLOSED (or never set): allow the request ──
        if state is None or state == CircuitState.CLOSED.value:
            return True

        # ── OPEN: check if the cooldown has elapsed ──
        if state == CircuitState.OPEN.value:
            # Read the timestamp when the circuit opened.
            opened_at = await redis.get(self._opened_key(key))
            if opened_at is None:
                # Stale state with no timestamp — treat as expired.
                # This can happen if the opened_at key was manually
                # deleted or expired before the state key.
                await self._transition(key, CircuitState.HALF_OPEN)
                return True

            # Calculate elapsed seconds since the circuit opened.
            elapsed = int(opened_at)
            import time
            if (int(time.time()) - elapsed) >= self.cooldown_seconds:
                # Cooldown expired → allow one test request (HALF_OPEN).
                await self._transition(key, CircuitState.HALF_OPEN)
                return True

            # Still within the cooldown — reject the request.
            return False

        # ── HALF_OPEN: allow exactly one test request ──
        # The caller that reaches here IS that test request.
        # If it succeeds → record_success() closes the circuit.
        # If it fails  → record_failure() re-opens the circuit.
        if state == CircuitState.HALF_OPEN.value:
            return True

        # Fallback: unknown state string — allow (fail-open).
        return True

    async def record_success(self, key: str) -> None:
        """
        Record a successful call. Resets the failure counter and
        closes the circuit (from HALF_OPEN or CLOSED).

        Call this **after** a successful API response:

            try:
                result = await africas_talking_client.send(...)
                await breaker.record_success("africas_talking")
            except Exception:
                await breaker.record_failure("africas_talking")

        Uses a Redis pipeline (``MULTI`` without ``EXEC`` transaction) to
        batch three writes into a single round-trip — more efficient than
        three separate ``SET``/``DELETE`` calls.
        """
        redis = get_redis_client()

        # Pipeline batches commands — they are sent together, reducing
        # network round-trips to Redis.
        pipe = redis.pipeline()
        pipe.set(self._state_key(key), CircuitState.CLOSED.value)
        pipe.delete(self._failures_key(key))
        pipe.delete(self._opened_key(key))
        await pipe.execute()

    async def record_failure(self, key: str) -> None:
        """
        Record a failed call. Increments the failure counter.

        Behaviour depends on the current state:

          - **CLOSED**: increment the failure count. If failures reach
            the threshold, open the circuit.
          - **HALF_OPEN**: the test request failed — re-open the circuit
            immediately (no need to count further).
          - **OPEN**: no-op (already open; the failure was likely a
            short-circuit rejection, not a real API attempt).

        Call this in the ``except`` block of your API call:

            try:
                result = await mpesa_adapter.stk_push(...)
                await breaker.record_success("mpesa")
            except Exception:
                await breaker.record_failure("mpesa")
        """
        redis = get_redis_client()

        state = await redis.get(self._state_key(key))

        # A failure in HALF_OPEN means the single test request failed.
        # Re-open the circuit immediately for another full cooldown.
        if state == CircuitState.HALF_OPEN.value:
            await self._open_circuit(key)
            return

        # ── CLOSED (or no state): increment the failure count ──
        # INCR initializes the key at 1 if it doesn't exist (Redis
        # auto-creates on first INCR). decode_responses=True means the
        # return is a str, so we cast to int.
        failures = int(await redis.incr(self._failures_key(key)))

        # If we've hit the threshold, trip the circuit to OPEN.
        if failures >= self.failure_threshold:
            await self._open_circuit(key)

    # ── Internal helpers ──────────────────────────────────────────────

    async def _open_circuit(self, key: str) -> None:
        """
        Transition to OPEN and record the current timestamp.

        Stores the Unix timestamp so ``can_execute()`` can later compute
        how long the circuit has been open and decide whether the
        cooldown has elapsed.

        The failure count key is *not* deleted here — it is retained so
        operators can inspect it with ``redis-cli`` for debugging. It
        will be reset on the next ``record_success()``.
        """
        import time
        redis = get_redis_client()
        pipe = redis.pipeline()
        pipe.set(self._state_key(key), CircuitState.OPEN.value)
        pipe.set(self._opened_key(key), int(time.time()))
        await pipe.execute()

    async def _transition(self, key: str, new_state: CircuitState) -> None:
        """
        Transition the circuit to *new_state*.

        This is a lightweight single-key SET. It is used for the
        OPEN → HALF_OPEN transition (after cooldown expires) and the
        HALF_OPEN → OPEN transition (after a failed test request).
        """
        redis = get_redis_client()
        await redis.set(self._state_key(key), new_state.value)


# ── Module-level singleton ────────────────────────────────────────────
# Safe to share because all state lives in Redis — the Python object
# holds only the threshold/cooldown config, which is read-only after
# __init__.
circuit_breaker = CircuitBreaker()