#!/usr/bin/env python3
"""
scripts/health_check.py — Celery worker health check
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Verifies that Celery workers are alive and processing by pinging the
Celery control plane over Redis. Intended for a Docker healthcheck or
Kubernetes liveness/readiness probe so the orchestrator can restart
an unhealthy worker container automatically.

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - "SMS Tuition Agent" → "SMS-First Agent" in the description and
    docstrings. The health check is Celery-app-specific, NOT
    domain-specific — it pings workers and counts active tasks, so it
    does not care whether the tenant table is called schools or
    businesses. The logic is therefore inherited verbatim.

Exit codes
----------
  0 — healthy   (at least one worker responded to ping)
  1 — unhealthy (no workers responded, or error contacting broker)

Usage
-----
  docker compose exec worker python -m scripts.health_check
  python scripts/health_check.py --redis-url redis://localhost:6379/0

TEACHING NOTES
--------------
  - We import `celery_app` from workers.celery_app so the health check
    uses the SAME broker config as the workers. No separate Redis
    connection is needed — Celery manages the broker connection.
  - `celery_app.control.inspect(timeout=5)` sends a ping with a 5s
    timeout. If no worker replies within 5s, we consider the cluster
    unhealthy. On a Pi this is generous; on a beefier host 2s would do.
  - The `--redis-url` arg is informational only (logged in details);
    Celery already knows its broker from celery_app.conf.broker_url.
    We keep the flag for ops scripts that report which broker was
    checked.
  - `inspect.ping()` returns a dict keyed by worker name → {"ok": ...}
    or None if no workers are online. `inspect.active()` returns a
    dict keyed by worker name → list of active task dicts.
  - A cluster is "healthy" only if at least one worker responded AND
    no transport errors occurred. This catches both "all workers
    dead" and "broker unreachable" failure modes.

KENYA-SPECIFIC CONSIDERATIONS
-----------------------------
  - On rural Kenyan networks, Redis and the workers share a Pi host,
    so broker latency is near-zero. The 5s timeout is plenty.
  - If the Pi's SD card degrades, Celery workers can hang silently.
    A health check that fails fast lets Docker restart the container
    before the whole system goes dark.

═══════════════════════════════════════════════════════════════════════
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from workers.celery_app import celery_app


async def check_worker_health(redis_url: str) -> dict[str, Any]:
    """Ping Celery workers and check active task count.

    Uses ``celery_app.control.inspect()`` which communicates with
    workers over the Redis broker. No direct Redis connection is
    needed — Celery manages the broker connection internally.

    Args:
        redis_url: Redis broker URL (for informational/logging purposes;
                   Celery already knows its broker from celery_app config).

    Returns:
        Dict with keys:
            - healthy (bool): True if at least one worker responded.
            - workers_online (int): Number of workers that responded to ping.
            - active_tasks (int): Total active tasks across all workers.
            - details (dict): Per-worker ping replies and active task lists.
    """
    details: dict[str, Any] = {
        "redis_url": redis_url,
        "ping": {},
        "active": {},
        "errors": [],
    }
    workers_online = 0
    active_tasks = 0

    # inspect() with no args targets all online workers.
    # timeout=5: how long to wait for a worker reply. On a Pi this is
    # generous; if workers are overloaded they may still reply slowly.
    inspect = celery_app.control.inspect(timeout=5)

    # ── Ping: "are you alive?" ──
    # Returns {worker_name: {"ok": "pong"}} or None if no workers.
    try:
        ping_replies = inspect.ping()
    except Exception as exc:
        details["errors"].append(f"ping failed: {exc!r}")
        ping_replies = None

    if ping_replies:
        details["ping"] = ping_replies
        workers_online = len(ping_replies)
    else:
        # ping_replies is None when no workers are online.
        details["ping"] = {}
        workers_online = 0

    # ── Active tasks: "what are you doing?" ──
    # Returns {worker_name: [task_dict, ...]} or None.
    try:
        active_replies = inspect.active()
    except Exception as exc:
        details["errors"].append(f"active() failed: {exc!r}")
        active_replies = None

    if active_replies:
        details["active"] = active_replies
        # Sum active task counts across all workers.
        for _worker, tasks in active_replies.items():
            if isinstance(tasks, list):
                active_tasks += len(tasks)
    else:
        details["active"] = {}

    # A cluster is healthy only if at least one worker is online AND
    # no transport errors occurred. This catches both "all workers
    # dead" and "broker unreachable" failure modes.
    healthy = workers_online > 0 and len(details["errors"]) == 0

    return {
        "healthy": healthy,
        "workers_online": workers_online,
        "active_tasks": active_tasks,
        "details": details,
    }


def _parse_args() -> argparse.Namespace:
    """Parse CLI args. Only --redis-url is supported (informational)."""
    parser = argparse.ArgumentParser(
        description="Health check for SMS-First Agent Celery workers.",
    )
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        help="Redis broker URL (default: $REDIS_URL or redis://localhost:6379/0)",
    )
    return parser.parse_args()


async def _main() -> int:
    """Entry point: run the health check and print JSON to stdout.

    Returns 0 (healthy) or 1 (unhealthy) so Docker/K8s can act on it.
    """
    args = _parse_args()
    result = await check_worker_health(args.redis_url)
    # default=str: serialize any non-JSON-native objects (e.g., datetime).
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))