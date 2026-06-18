"""
scheduler package — Celery Beat scheduling
═══════════════════════════════════════════

The actual Beat schedule lives in workers/celery_app.py (see
`celery_app.conf.beat_schedule`). This package is reserved for:
  - Custom scheduler classes (if we move beyond Celery Beat)
  - Business-configurable schedule overrides stored in DB
  - Runbook documentation for cron-like scheduling

See scheduler/beat_schedule.py for the schedule definition and rationale.
"""