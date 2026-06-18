"""
infra/connectivity_watcher.py — Offline detection for unreliable networks
═══════════════════════════════════════════════════

In rural Kenya, internet connectivity is intermittent. The SMS-first
agent must continue operating offline and resume sending when
connectivity returns.

This module:
  1. Pings Africa's Talking API every 30 seconds
  2. Sets a Redis flag `connectivity:online` (true/false)
  3. When offline: send workers skip (messages stay PENDING in outbox)
  4. When online: send workers resume, reconciliation runs for stuck messages

The outbox pattern makes this seamless:
  - Offline: messages accumulate as PENDING (safe, no sends attempted)
  - Online: poll_pending() picks them up and sends them in order

Teaching notes:
  - We use Redis for the connectivity flag because it's fast and shared
    across all worker processes. No DB query needed for every send.
  - The ping target is Africa's Talking's balance API — it's lightweight
    and also alerts us if our API key is invalid.
  - We don't block on network calls. If the ping times out in 5 seconds,
    we mark offline immediately. No hanging threads.
  - The watcher runs as a Celery Beat task (every 30 seconds), not a
    separate thread. This keeps the architecture simple.
═══════════════════════════════════════════════════
"""

import asyncio
import logging
from datetime import datetime

import httpx
from infra.audit_logger import AuditContext, log_audit_event
from infra.redis_pool import get_redis_client
from infra.settings import get_settings

logger = logging.getLogger(__name__)

# Redis key for connectivity status
CONNECTIVITY_KEY = "connectivity:online"
CONNECTIVITY_LAST_CHECK = "connectivity:last_check"

# Africa's Talking balance endpoint (lightweight, validates API key)
AT_BALANCE_URL = "https://api.africastalking.com/version1/ balance"


async def check_connectivity() -> bool:
    """
    Ping Africa's Talking API to check if we have internet + API access.

    Returns True if online, False if offline.
    """
    settings = get_settings()
    headers = {
        "apiKey": settings.africas_talking_api_key.get_secret_value(),
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(AT_BALANCE_URL, headers=headers)
            if response.status_code == 200:
                return True
            # Non-200 might mean API issue but network is up
            # We'll treat 4xx as "online" (network works, API key issue is separate)
            if response.status_code < 500:
                return True
            return False
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError):
        return False
    except Exception as exc:
        logger.warning(f"Connectivity check failed: {exc}")
        return False


async def update_connectivity_status() -> dict:
    """
    Celery task: check connectivity and update Redis flag.

    Called every 30 seconds by Celery Beat.

    Returns:
        {"online": bool, "previous": bool, "changed": bool}
    """
    redis = get_redis_client()

    # Get previous status
    previous_str = await redis.get(CONNECTIVITY_KEY)
    previous = previous_str == "true"

    # Check current status
    current = await check_connectivity()

    # Update Redis
    await redis.set(CONNECTIVITY_KEY, "true" if current else "false")
    await redis.set(CONNECTIVITY_LAST_CHECK, datetime.utcnow().isoformat())

    # Log state transitions
    if current != previous:
        if current:
            logger.info("Connectivity restored — resuming SMS sends")
            await log_audit_event(
                event_type="system.connectivity_restored",
                entity_type="system",
                entity_id=None,
                summary="Internet connectivity restored, SMS sends resuming",
                context=AuditContext(actor_type="system", actor_id="connectivity_watcher"),
            )
        else:
            logger.warning("Connectivity lost — pausing SMS sends")
            await log_audit_event(
                event_type="system.connectivity_lost",
                entity_type="system",
                entity_id=None,
                summary="Internet connectivity lost, SMS sends paused",
                context=AuditContext(actor_type="system", actor_id="connectivity_watcher"),
            )

    return {"online": current, "previous": previous, "changed": current != previous}


async def is_online() -> bool:
    """
    Check if we're currently online. Used by send workers.

    Returns True if online (Redis says true), False otherwise.
    If Redis is down, assume online (fail open — better to attempt send
    and let the adapter handle the timeout).
    """
    try:
        redis = get_redis_client()
        status = await redis.get(CONNECTIVITY_KEY)
        if status is None:
            # No check has run yet — assume online
            return True
        return status == "true"
    except Exception:
        # Redis down — fail open
        return True