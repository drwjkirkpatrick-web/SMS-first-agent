"""
workers/celery_app.py — Celery application configuration
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Creates and configures the Celery application that runs all background
tasks: reminder computation, SMS sending, reconciliation, inbound
parsing, campaign processing, and M-Pesa payment matching.

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - App name: "sms_tuition_agent" → "sms_first_agent"
  - Timezone: "UTC" → "Africa/Nairobi" (EAT, UTC+3, no DST in Kenya)
  - Beat schedule: 8 AM UTC → 6 AM EAT (morning reminder computation)
  - Added task routes for campaigns + mpesa_reconciliation queues
  - Added new task modules to the include list
  - task_acks_late=True + worker_prefetch_multiplier=1 (unchanged —
    critical for the SKIP LOCKED outbox pattern on Pi hardware)

KEY DESIGN DECISIONS
--------------------
  1. Redis as both broker and result backend. Redis runs locally on
     the Pi (same Docker network). This avoids an external dependency
     and keeps latency low.
  2. Separate queues per task type. This lets us scale workers
     independently: `docker compose up -d --scale worker=3` scales
     the sends queue without touching reconciliation.
  3. task_acks_late=True means a task is only acknowledged AFTER it
     completes. If a worker crashes mid-task, the task is redelivered
     to another worker. Combined with the outbox state machine, this
     guarantees at-least-once delivery with no duplicates.
  4. worker_prefetch_multiplier=1 means each worker process takes
     only ONE task at a time. On Pi (limited RAM), this prevents
     memory exhaustion from buffering many tasks.
  5. The Beat schedule lives HERE (not in scheduler/beat_schedule.py)
     because Celery reads it from celery_app.conf.beat_schedule. The
     scheduler/ package is reserved for future dynamic schedule loading.

TEACHING NOTES
--------------
  - `@celery_app.task` registers a function as a background task.
  - Celery workers are separate processes from the FastAPI server.
    They communicate via Redis (the broker), not via in-memory calls.
  - The `include` list tells Celery which modules to import so it can
    find @task-decorated functions. We list all worker modules.
  - `autodiscover_tasks()` also scans the workers package for tasks.
  - The beat_schedule dict maps human-readable names to task configs.
    Each entry has: task (dotted path), schedule (crontab or seconds),
    and optional kwargs/args.

KENYA-SPECIFIC CONSIDERATIONS
-----------------------------
  - Africa/Nairobi timezone (EAT, UTC+3). Kenya does NOT observe
    daylight saving time, so crontab schedules are stable year-round.
  - The reminder task runs at 6 AM EAT — before most businesses open
    at 7 AM, so reminders are in the outbox ready to send at opening.
  - The connectivity check runs every 30 seconds — in rural Kenya,
    internet can drop and recover frequently. Quick detection means
    the send worker pauses and resumes promptly.
  - All queues are processed locally (no cross-region routing).
═══════════════════════════════════════════════════════════════════════
"""

import os

from celery import Celery
from celery.schedules import crontab

# ── Redis broker URL ──────────────────────────────────────────────
# Celery uses Redis as the message broker (who gets which task) and
# the result backend (stores task return values). Both run locally
# on the Pi via Docker Compose.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ── Celery App ────────────────────────────────────────────────────
# The app name "sms_first_agent" identifies this Celery instance.
# Workers connect to the broker with: celery -A workers.celery_app worker
celery_app = Celery(
    "sms_first_agent",
    broker=REDIS_URL,
    backend=REDIS_URL,
    # List all worker modules so Celery can find @task functions.
    include=[
        "workers.reminders",
        "workers.sends",
        "workers.reconciliation",
        "workers.inbound",
        "workers.campaigns",
        "workers.mpesa_reconciliation",
    ],
)

# ── Configuration ──────────────────────────────────────────────────
celery_app.conf.update(
    # Results expire after 1 hour (free up Redis memory on Pi).
    result_expires=3600,
    # JSON serialization for all messages (safe, debuggable).
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # Kenya timezone — Beat schedules evaluate in Africa/Nairobi (EAT).
    # enable_utc=True stores internal timestamps in UTC, but crontab
    # entries are interpreted in the timezone below.
    timezone="Africa/Nairobi",
    enable_utc=True,
    # ── Task routing: separate queues per task type ──
    # This lets us scale workers per queue. For example, if sends are
    # the bottleneck, scale the sends queue: `--scale worker=4 -Q sends`.
    task_routes={
        "workers.reminders.*": {"queue": "reminders"},
        "workers.sends.*": {"queue": "sends"},
        "workers.reconciliation.*": {"queue": "reconciliation"},
        "workers.inbound.*": {"queue": "inbound"},
        "workers.campaigns.*": {"queue": "campaigns"},
        "workers.mpesa_reconciliation.*": {"queue": "mpesa"},
    },
    # ── Retry behavior ──
    # Default retry delay (seconds). Individual tasks can override.
    task_default_retry_delay=60,
    task_max_retries=3,
    # ── Critical for outbox pattern on Pi ──
    # acks_late: task is acknowledged ONLY after successful completion.
    # If a worker crashes mid-task, the task is redelivered to another
    # worker. Combined with the outbox state machine, this guarantees
    # at-least-once delivery with no duplicate SMS.
    task_acks_late=True,
    # prefetch=1: each worker process takes only ONE task at a time.
    # On Raspberry Pi (4GB RAM), buffering many tasks can exhaust memory.
    # This setting ensures predictable memory usage per worker.
    worker_prefetch_multiplier=1,
)

# ── Beat Schedule ─────────────────────────────────────────────────
# Celery Beat is the scheduler that triggers periodic tasks.
#
# All times are in Africa/Nairobi (EAT, UTC+3) because we set
# timezone="Africa/Nairobi" above. Kenya has NO daylight saving,
# so these schedules are stable year-round.
#
# Schedule rationale:
#   6:00 AM EAT  — compute reminders (before business opens at 7 AM)
#   every 2 min  — poll outbox + send (fast delivery, catches backlog)
#   every 5 min  — reconcile unknown deliveries (rural network timeouts)
#   hourly       — poll payment updates (M-Pesa + CSV sync)
#   hourly       — process scheduled campaigns (check if campaign is due)
#   every 30 sec — connectivity check (rural Kenya: quick offline detection)
celery_app.conf.beat_schedule = {
    # ── Daily reminder computation at 6 AM EAT ──
    # The reminder worker loads all active transactions, computes which
    # reminders are due today, and inserts them into the outbox.
    # Running at 6 AM means reminders are ready to send when the business
    # opens at 7 AM (the send worker respects business hours).
    "compute-reminders-daily": {
        "task": "workers.reminders.compute_reminder_candidates",
        "schedule": crontab(hour=6, minute=0),  # 6:00 AM EAT
        "kwargs": {"business_id": 1},  # default: first business
    },
    # ── Poll outbox and send SMS every 2 minutes ──
    # The send worker claims pending messages, sends via Africa's Talking
    # (or Twilio fallback), and transitions status. Fast polling means
    # messages are delivered within 2 minutes of being scheduled.
    "poll-and-send": {
        "task": "workers.sends.poll_and_send_messages",
        "schedule": 120.0,  # every 2 minutes
    },
    # ── Reconcile unknown deliveries every 5 minutes ──
    # Messages in UNKNOWN_DELIVERY state (ambiguous send result) are
    # queried at the provider. Rural Kenya has frequent network timeouts,
    # so this runs more frequently than the original (10 min → 5 min).
    "reconcile-unknown": {
        "task": "workers.reconciliation.reconcile_unknown_deliveries",
        "schedule": 300.0,  # every 5 minutes
    },
    # ── Poll payment updates hourly ──
    # Syncs payments from CSV connector and checks for M-Pesa payments
    # that haven't been matched yet.
    "poll-payments": {
        "task": "workers.reconciliation.poll_payment_updates",
        "schedule": crontab(minute=30),  # at :30 of every hour
        "kwargs": {"business_id": 1},
    },
    # ── Process scheduled campaigns hourly ──
    # Checks for campaigns in SCHEDULED status whose schedule_start time
    # has arrived, and triggers the campaign worker to build candidates.
    "process-scheduled-campaigns": {
        "task": "workers.campaigns.process_scheduled_campaigns",
        "schedule": crontab(minute=15),  # at :15 of every hour
    },
    # ── Connectivity check every 30 seconds ──
    # Pings Africa's Talking API endpoint. If unreachable, pauses the
    # send worker (messages stay PENDING in outbox). When connectivity
    # returns, the send worker resumes and flushes the backlog.
    "connectivity-check": {
        "task": "workers.sends.check_connectivity",
        "schedule": 30.0,  # every 30 seconds
    },
}

# Auto-discover tasks in the workers package.
# This finds any @celery_app.task or @shared_task decorated functions
# that we might have missed in the `include` list above.
celery_app.autodiscover_tasks()