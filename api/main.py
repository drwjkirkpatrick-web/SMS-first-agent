"""
api/main.py — FastAPI Application Entry Point
═══════════════════════════════════════════════════

The main FastAPI application for the SMS-First Agent.

Adapted from the original tuition agent's main.py:
  - Added Africa's Talking webhook router
  - Added M-Pesa webhook router
  - Kept Twilio webhook router (fallback provider)
  - Added admin router (dashboard, campaigns, customer search)
  - CORS middleware for web dashboard access
  - Startup/shutdown lifecycle events

Key features:
  - Health check endpoint (/health) — verifies DB + Redis connectivity
  - Webhook routers for all providers (Africa's Talking, M-Pesa, Twilio)
  - Admin router for dashboard and management endpoints
  - CORS for web dashboard (if one exists)
  - Lifespan context manager for DB/Redis initialization

Teaching notes:
  - FastAPI uses "lifespan" (async context manager) for startup/shutdown.
    This replaces the older @app.on_event("startup") pattern.
  - We mount routers with `include_router()`. The `prefix` parameter
    adds a path prefix to all routes in the router (e.g., /webhooks/at/inbound).
  - The health check returns 503 if DB or Redis is unreachable, so
    Docker/k8s can detect unhealthy containers and restart them.
  - CORS is configured to allow the admin dashboard origin. In production,
    restrict this to known origins only.

Kenya-specific considerations:
  - The app runs on ARM64 (Raspberry Pi). FastAPI + uvicorn is efficient
    on Pi hardware. Use 1-2 workers (Pi has limited CPU cores).
  - All endpoints use async (non-blocking) I/O — critical on Pi where
    blocking one request would block all others.
  - The admin dashboard tracks SMS spend in KES — the admin endpoints
    provide the data for this.
═══════════════════════════════════════════════════
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.webhooks.africas_talking import router as at_webhook_router
from api.webhooks.mpesa import router as mpesa_webhook_router
from api.webhooks.twilio import router as twilio_webhook_router
from api.admin import router as admin_router
from infra.database import close_db, init_db
from infra.redis_pool import close_redis, ping_redis
from infra.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager.

    Runs on startup: initialize DB (in dev mode), verify Redis.
    Runs on shutdown: close DB and Redis connections.

    Teaching note: In development mode, we auto-create tables with
    `init_db()` for convenience. In production, use Alembic migrations
    instead (never auto-create in prod — schema changes must be
    explicit and reversible).
    """
    settings = get_settings()

    # In development mode, auto-create database tables
    if settings.app_env == "development":
        await init_db()

    # Verify Redis is reachable — Celery needs it for task queue
    # We warn but don't crash — the app can still serve health checks
    # even if Redis is temporarily down (Celery workers will retry).
    redis_ok = await ping_redis()
    if not redis_ok:
        import logging
        logging.getLogger(__name__).warning(
            "Redis is unreachable on startup — Celery workers may fail. "
            "Check REDIS_URL in .env"
        )

    yield  # application runs here

    # Shutdown: clean up connections
    await close_db()
    await close_redis()


# ── FastAPI App ─────────────────────────────────────────────────

app = FastAPI(
    title="SMS-First Agent",
    description=(
        "Headless SMS-first customer engagement platform for Kenyan "
        "small businesses. Sends reminders, promotions, loyalty updates, "
        "and payment follow-ups via SMS. Integrates M-Pesa for payments."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS Middleware ──────────────────────────────────────────────
#
# Cross-Origin Resource Sharing: allows web applications from other
# origins to call our API. In production, restrict `allow_origins`
# to known dashboard URLs only. In development, allow all origins.

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict to dashboard URL in production
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)


# ── Health Check ────────────────────────────────────────────────

@app.get("/health", tags=["health"])
async def health_check():
    """
    Health check endpoint for container orchestration.

    Checks:
      - PostgreSQL connectivity (SELECT 1)
      - Redis connectivity (ping)

    Returns 200 if healthy, 503 if any component is down.
    Docker/k8s uses this to determine if the container should receive
    traffic or be restarted.
    """
    from infra.database import check_db_connection, get_engine
    from sqlalchemy import text

    try:
        db_ok = await check_db_connection()
        db_status = "connected" if db_ok else "disconnected"
    except Exception:
        db_status = "disconnected"

    from infra.redis_pool import check_redis_connection
    redis_ok = await check_redis_connection()
    redis_status = "connected" if redis_ok else "disconnected"

    # Overall health: both DB and Redis must be connected
    overall = "healthy" if db_status == "connected" and redis_status == "connected" else "unhealthy"
    code = (
        status.HTTP_200_OK
        if overall == "healthy"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )

    return JSONResponse(
        status_code=code,
        content={
            "status": overall,
            "version": "0.1.0",
            "db": db_status,
            "redis": redis_status,
        },
    )


# ── Router Mounting ─────────────────────────────────────────────
#
# Each router is mounted with a prefix:
#   /webhooks/at/*      — Africa's Talking webhooks (inbound SMS, delivery)
#   /webhooks/mpesa/*   — M-Pesa webhooks (C2B confirmation, STK callback)
#   /webhooks/twilio/*  — Twilio webhooks (status callback, fallback provider)
#   /admin/*            — Admin dashboard endpoints (stats, campaigns, search)

app.include_router(at_webhook_router, prefix="/webhooks", tags=["webhooks"])
app.include_router(mpesa_webhook_router, prefix="/webhooks", tags=["webhooks"])
app.include_router(twilio_webhook_router, prefix="/webhooks", tags=["webhooks"])
app.include_router(admin_router, prefix="/admin", tags=["admin"])


# ── Root ────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    """Root endpoint — redirects to API docs."""
    return {
        "message": "SMS-First Agent — see /docs for API documentation",
        "version": "0.1.0",
    }