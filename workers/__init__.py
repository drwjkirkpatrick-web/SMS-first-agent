"""
workers package — Celery background task workers
═══════════════════════════════════════════════════

This package contains all Celery background tasks for the SMS-First Agent.
Each module is auto-discovered by the Celery app (see celery_app.py).

Modules:
  - celery_app.py          Celery app instance + configuration
  - reminders.py            Daily reminder candidate computation
  - sends.py                Outbox polling + SMS dispatch
  - reconciliation.py       Unknown delivery resolution + payment polling
  - inbound.py              Inbound SMS keyword parser + intent dispatch
  - campaigns.py            Promotional campaign processing (NEW)
  - mpesa_reconciliation.py M-Pesa payment matching (NEW)

Kenya-specific considerations:
  - All tasks run on ARM64 (Raspberry Pi) with limited CPU/RAM.
  - Worker prefetch is set to 1 (one task at a time per worker process)
    to avoid memory pressure on the Pi.
  - The connectivity watcher pauses sends when offline (rural Kenya).
  - Quiet hours + business hours are enforced in the send worker.
═══════════════════════════════════════════════════
"""

# Re-export tasks for convenient importing and Celery autodiscovery.
from workers.reminders import compute_reminder_candidates  # noqa: F401
from workers.sends import poll_and_send_messages  # noqa: F401
from workers.reconciliation import (  # noqa: F401
    reconcile_unknown_deliveries,
    poll_payment_updates,
)
from workers.inbound import process_inbound_message  # noqa: F401
from workers.campaigns import process_campaign  # noqa: F401
from workers.mpesa_reconciliation import process_mpesa_payment  # noqa: F401