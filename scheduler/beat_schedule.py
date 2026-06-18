"""
scheduler/beat_schedule.py — Celery Beat schedule definition
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Documents the Celery Beat schedule and provides a reference for the
actual schedule that lives in workers/celery_app.py.

The actual schedule is set via `celery_app.conf.beat_schedule` in
workers/celery_app.py. This file documents the rationale and provides
a place for future dynamic schedule loading (e.g., business-configurable
reminder times stored in the database).

SCHEDULE OVERVIEW
------------------
  ┌────────────────────────────────┬──────────────────────┬─────────────────────────┐
  │ Task                           │ Schedule             │ Queue                   │
  ├────────────────────────────────┼──────────────────────┼─────────────────────────┤
  │ compute_reminder_candidates    │ Daily 6:00 AM EAT    │ reminders               │
  │ poll_and_send_messages         │ Every 2 minutes      │ sends                   │
  │ reconcile_unknown_deliveries   │ Every 5 minutes      │ reconciliation           │
  │ poll_payment_updates           │ Hourly at :30        │ reconciliation           │
  │ process_scheduled_campaigns     │ Hourly at :15        │ campaigns                │
  │ check_connectivity             │ Every 30 seconds     │ sends                   │
  └────────────────────────────────┴──────────────────────┴─────────────────────────┘

TIMEZONE
--------
All times are in Africa/Nairobi (EAT, UTC+3). Kenya does NOT observe
daylight saving time, so the schedule is stable year-round.

RATIONALE
---------
  - 6 AM EAT for reminders: before businesses open at 7 AM, so reminders
    are in the outbox ready to send at opening.
  - 2 min for sends: fast enough for near-real-time delivery, but not
    so frequent that it overloads the Pi.
  - 5 min for reconciliation: rural Kenya has frequent network timeouts;
    resolving UNKNOWN_DELIVERY quickly improves customer experience.
  - Hourly for payment updates: M-Pesa webhooks are real-time, but CSV
    sync and unmatched payment checks run hourly.
  - 30 sec for connectivity: quick offline detection means the send
    worker pauses and resumes promptly in rural areas with flaky internet.

FUTURE: DYNAMIC SCHEDULE LOADING
--------------------------------
In Phase 2, the schedule could be made business-configurable:
  - A business could set their reminder time (e.g., 7 AM instead of 6 AM).
  - A business could configure their quiet hours and business hours.
  - The scheduler would load these from the business's reminder_policy
    JSON and override the default Beat schedule per business.

This would require a custom Beat scheduler class that reads from the
database. For now, the schedule is static and applies to all businesses.

TEACHING NOTES
--------------
  - Celery Beat is a separate process from the workers. It runs as its
    own Docker container (see docker-compose.yml).
  - Beat uses the `sqlalchemy` scheduler by default, which stores
    last-run times in the database. This means if Beat restarts, it
    knows which tasks it already ran today and doesn't double-fire.
  - In a multi-Pi deployment (redundancy), only ONE Beat process should
    run. The sqlalchemy scheduler prevents duplicate task firing even
    if two Beat instances are accidentally started.
═══════════════════════════════════════════════════════════════════════
"""

# The actual beat schedule is defined in workers/celery_app.py.
# This file is documentation only.
#
# To view the current schedule at runtime:
#   from workers.celery_app import celery_app
#   print(celery_app.conf.beat_schedule)
#
# To override the schedule (e.g., for testing):
#   celery_app.conf.beat_schedule = {
#       "test-reminder": {
#           "task": "workers.reminders.compute_reminder_candidates",
#           "schedule": 10.0,  # every 10 seconds for testing
#           "kwargs": {"business_id": 1},
#       },
#   }