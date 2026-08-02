"""
workers/maintenance.py — Scheduled maintenance Celery tasks
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Defines three periodic Celery tasks that keep the SMS-First Agent
healthy without manual intervention:

  R6  run_retention_purge  — purge expired audit/log rows daily at 3 AM.
                             Protects the Pi's SD card from unbounded
                             growth of the audit_events table.
  R9  run_alert_check      — scan recent failure rates every 15 minutes
                             and fire an alert if the threshold is
                             exceeded. Catches provider outages early.
  R10 run_backup           — encrypted pg_dump nightly at 2 AM. The
                             safety net for a Pi SD card failure.

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - school_id → business_id everywhere (the tenant column was renamed
    when the School model became Business). This is the ONLY structural
    change — the task bodies, retry configs, and audit logging are
    inherited verbatim because the maintenance logic is domain-agnostic:
    it operates on tables (audit_events, outbound_messages) whose names
    and shapes did not change.
  - The default business_id=1 in run_alert_check mirrors the original
    school_id=1 default — the first tenant row in single-tenant deploys.

KEY DESIGN DECISIONS (inherited, unchanged)
-------------------------------------------
  1. Each task is a thin sync wrapper that calls asyncio.run(...) on an
     async helper. Celery tasks are sync by default; this pattern lets
     us reuse the async domain services (which use asyncpg sessions)
     without re-writing them as sync code.
  2. Imports are deferred (inside the function body) so that importing
     this module never triggers a DB connection. Celery imports every
     module in its `include` list at worker startup; if maintenance.py
     imported domain.retention at module scope, a missing DB env var
     would crash the ENTIRE worker, not just the maintenance task.
  3. bind=True gives the task access to `self` for retry control.
  4. max_retries and default_retry_delay are tuned per task:
       - retention purge: 3 retries, 5 min backoff (idempotent, safe to retry)
       - alert check:     3 retries, 2 min backoff (frequent, low cost)
       - backup:          1 retry,  10 min backoff (expensive; retry once)

TEACHING NOTES
--------------
  - `@celery_app.task` registers the function on the Celery app defined
    in workers/celery_app.py. Beat then schedules it per beat_schedule.
  - The beat schedule for these tasks is added by the deployment's
    beat_schedule config (the original tuition agent put them in
    scheduler/beat_schedule.py; here they may live in celery_app.conf
    or a dedicated scheduler module).
  - AuditContext(business_id=...) is a dataclass (see infra/audit_logger.py).
    It carries tenant + actor metadata so log_audit_event can write a
    compliant audit row in one call. The v2 AuditContext field is
    `business_id` (renamed from the tuition agent's `school_id`).
  - AuditEventType.SIS_SYNC is reused as the closest existing audit type
    for "system maintenance ran". In a future migration this could
    become AuditEventType.RETENTION_PURGE etc., but reusing avoids a
    model/migration change for this port.
  - These tasks return dicts so Celery result inspection (and the health
    dashboard) can show what each run did without reading the logs.

KENYA-SPECIFIC CONSIDERATIONS
-----------------------------
  - The backup task writes to /data/backups (a Docker volume mounted on
    external storage). On a Pi, the SD card is unreliable; backups MUST
    land on a separate device to be a real safety net.
  - The retention purge respects the Kenya Data Protection Act (2019):
    personal data must not be retained longer than necessary. The
    retention windows are configured in domain/retention.py.

═══════════════════════════════════════════════════════════════════════
"""

from workers.celery_app import celery_app


# ── R6: Data Retention Purge ──────────────────────────────────────
# Runs daily (Beat: 3 AM). Purges audit_events and other log rows older
# than the configured retention window. Idempotent — safe to retry.
@celery_app.task(bind=True, max_retries=3, default_retry_delay=300)
def run_retention_purge(self, business_id: int = 0) -> dict:
    """R6: Run data retention purge for all record types.

    Args:
        business_id: Tenant to purge for. 0 means "all businesses"
            (the helper treats <=0 as None, i.e. no tenant filter).
            Single-tenant deploys pass 0; multi-tenant deploys call
            this once per active business from Beat.

    Returns:
        Dict describing what was purged (counts per table), suitable
        for Celery result inspection and the admin dashboard.
    """
    import asyncio
    return asyncio.run(_async_run_retention_purge(business_id))


async def _async_run_retention_purge(business_id: int = 0) -> dict:
    """Async body of run_retention_purge (see task docstring above).

    Deferred imports keep this module import-safe at Celery startup.
    """
    from domain.retention import RetentionService
    from domain.models import AuditEventType
    from infra.audit_logger import AuditContext, log_audit_event

    svc = RetentionService()
    # Normalize: 0 (or negative) → None → "no tenant filter, purge all".
    # This mirrors the tuition agent's `school_id if school_id > 0 else None`.
    bid = business_id if business_id > 0 else None
    result = await svc.run_retention_purge(bid)

    # Record that the purge ran, for compliance audit trail.
    # AuditContext.business_id is the v2 field name (was school_id).
    #
    # NOTE: AuditEventType.SIS_SYNC is inherited from the tuition agent as
    # the "closest existing audit type" for system maintenance. If the v2
    # AuditEventType enum does not include SIS_SYNC, either add it to
    # domain/models.py or substitute the closest v2 value (e.g.,
    # POLICY_CHANGED). The log_audit_event signature accepts a str, so a
    # raw string fallback "sis.sync" also works if the DB enum permits it.
    await log_audit_event(
        event_type=AuditEventType.SIS_SYNC,  # closest existing audit type
        entity_type="system",
        entity_id="retention_purge",
        summary=f"Retention purge: {result}",
        context=AuditContext(business_id=bid, actor_type="system"),
    )
    return result


# ── R9: Failure Threshold Alerting ────────────────────────────────
# Runs every 15 min (Beat). Scans the failure rate of recent outbound
# messages; if it exceeds the threshold, fires an alert (log + notify).
@celery_app.task(bind=True, max_retries=3, default_retry_delay=120)
def run_alert_check(self, business_id: int = 1) -> dict:
    """R9: Check failure rate and alert if threshold exceeded.

    Args:
        business_id: Tenant to check. Defaults to 1 (the first
            business in single-tenant deploys), matching the original
            tuition agent's school_id=1 default.

    Returns:
        Dict with the computed failure rate, threshold, and whether
        an alert was fired. Consumed by the admin dashboard.
    """
    import asyncio
    return asyncio.run(_async_run_alert_check(business_id))


async def _async_run_alert_check(business_id: int) -> dict:
    """Async body of run_alert_check (see task docstring above)."""
    from domain.alerting import AlertService

    svc = AlertService()
    # AlertService is tenant-scoped: it reads only this business's
    # outbound messages to compute the failure rate.
    result = await svc.run_alert_check(business_id)
    return result


# ── R10: Automated Database Backup ────────────────────────────────
# Runs nightly (Beat: 2 AM). Creates an encrypted pg_dump and prunes
# old backups per the retention policy. Only retries once because each
# attempt is expensive (a full DB dump).
@celery_app.task(bind=True, max_retries=1, default_retry_delay=600)
def run_backup(self) -> dict:
    """R10: Run automated database backup.

    No arguments — backups are global (the whole DB), not per-tenant.
    Delegates to infra.backup.run_backup_job, which reads settings for
    the DB URL, encryption key, and output directory.

    Returns:
        Dict with the backup file path, count of old backups deleted,
        and a timestamp. The encryption key itself is never included.
    """
    import asyncio
    return asyncio.run(_async_run_backup())


async def _async_run_backup() -> dict:
    """Async body of run_backup (see task docstring above)."""
    from infra.backup import run_backup_job

    # run_backup_job pulls settings (DB URL, encryption key, output dir)
    # internally, so we don't need to pass anything here. It returns a
    # dict suitable for Celery result inspection.
    result = await run_backup_job()
    return result