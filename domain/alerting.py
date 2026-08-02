"""
domain/alerting.py — Failure threshold alerting for SMS delivery
═══════════════════════════════════════════════════

Monitors SMS send failure rates and alerts business owners (or their
admin contacts) when the failure rate exceeds a configurable threshold.

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - School → Business, school_id → business_id, schools → businesses
  - Guardian → Contact, guardian_id → contact_id, guardians → contacts
  - Primary SMS provider is Africa's Talking (NOT Twilio).
    Alerts go out via `adapters.africas_talking.py`, the same adapter
    the send worker uses, so alerts ride the same provider + dedup
    pipeline. Twilio remains available as a fallback only.
  - Monetary / cost references are in KES, not USD.
  - Default timezone context is Africa/Nairobi (EAT, UTC+3), not
    America/Los_Angeles.
  - The admin alert phone is read from the ALERT_ADMIN_PHONE env var
    (per-business owner config). In production this would be a column
    on the businesses table or a dedicated admin-contact config, but
    the env-var approach is inherited from the tuition agent and kept
    for simplicity in the first phase.

Typical deployment: a Celery beat task calls run_alert_check(business_id)
every 15–60 minutes. If the failure rate in the last window exceeds
the threshold (default 20%), an SMS alert is sent to the owner/admin phone.

AlertResult is a plain dataclass so it can be serialised in Celery
task return values and API responses without ORM session entanglement.

TEACHING NOTES
--------------
  - We only count messages that reached a terminal state (SENT,
    DELIVERED, FAILED) — in-flight messages (PENDING, SENDING,
    UNKNOWN_DELIVERY) are excluded from both numerator and denominator.
    Counting a PENDING message in the denominator would artificially
    lower the failure rate; counting it in the numerator is impossible
    because it hasn't failed yet.
  - The threshold is a percentage expressed as a float (0.20 = 20%).
  - send_alert() uses the same Africa's Talking adapter as the send
    worker, so alerts go through the same provider and the same
    outbox deduplication pipeline.
  - The admin alert phone is read from the ALERT_ADMIN_PHONE env var.
    In production, this would be a per-business setting stored in the
    businesses table (e.g., an `admin_alert_phone` column) or a
    dedicated admin config table.
  - AuditContext in this codebase keeps the legacy field name
    `school_id` (see infra/audit_logger.py) but is used as
    business_id in practice — we pass business_id into that field.
═══════════════════════════════════════════════════
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import AuditEventType, Business, MessageStatus, OutboundMessage
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory


# ═══════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════

@dataclass(frozen=True)
class AlertResult:
    """
    Result of a failure-rate check.

    Attributes:
        should_alert: True if failure_rate >= threshold and total_sent >= min_sample
        failure_rate: fraction of terminal messages that failed (0.0–1.0)
        total_sent: total messages that reached a terminal state in the window
        total_failed: number of those that failed
    """
    should_alert: bool
    failure_rate: float
    total_sent: int
    total_failed: int


# ═══════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════

# Minimum sample size before we alert — avoids noise when traffic is very low.
# If a business only sends 3 messages an hour and 1 fails, that's 33% — but
# not statistically meaningful. We wait until at least 10 terminal messages
# have accumulated before the threshold is evaluated.
_MIN_SAMPLE_SIZE = 10


class AlertService:
    """
    Checks SMS failure rates and sends alerts to business owners / admins.

    All session-accepting methods participate in the caller's transaction
    (they don't commit or rollback — that's the caller's job).
    run_alert_check() manages its own session and is safe to call from Celery.
    """

    async def check_failure_rate(
        self,
        session: AsyncSession,
        business_id: int,
        window_hours: int = 1,
        threshold_pct: float = 0.20,
    ) -> AlertResult:
        """
        Query recent outbound messages and compute the failure rate.

        Only messages in a terminal state (SENT, DELIVERED, FAILED) are
        counted. PENDING, SENDING, and UNKNOWN_DELIVERY are excluded
        because their outcome is not yet determined.

        Args:
            session: active async DB session
            business_id: business to check (was school_id in tuition agent)
            window_hours: look-back window in hours (default 1)
            threshold_pct: alert threshold as a fraction (0.20 = 20%)

        Returns:
            AlertResult with computed metrics and should_alert flag
        """
        cutoff = datetime.utcnow() - timedelta(hours=window_hours)

        # Count failed messages in the window
        failed_result = await session.execute(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.business_id == business_id,
                OutboundMessage.status == MessageStatus.FAILED,
                OutboundMessage.updated_at >= cutoff,
            )
        )
        total_failed = failed_result.scalar() or 0

        # Count all terminal-state messages in the window
        # (SENT + DELIVERED + FAILED = everything that reached a final outcome)
        terminal_statuses = [
            MessageStatus.SENT,
            MessageStatus.DELIVERED,
            MessageStatus.FAILED,
        ]
        total_result = await session.execute(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.business_id == business_id,
                OutboundMessage.status.in_(terminal_statuses),
                OutboundMessage.updated_at >= cutoff,
            )
        )
        total_sent = total_result.scalar() or 0

        # Compute failure rate (guard against division by zero)
        failure_rate = total_failed / total_sent if total_sent > 0 else 0.0

        # Only alert if we have enough data AND the rate exceeds threshold
        should_alert = (
            total_sent >= _MIN_SAMPLE_SIZE
            and failure_rate >= threshold_pct
        )

        return AlertResult(
            should_alert=should_alert,
            failure_rate=round(failure_rate, 4),
            total_sent=total_sent,
            total_failed=total_failed,
        )

    async def send_alert(
        self,
        business_name: str,
        phone: str,
        failure_rate: float,
    ) -> None:
        """
        Send an SMS alert to a business owner / admin about high failure rates.

        Uses the same Africa's Talking adapter as the send worker so alerts
        go through the same provider infrastructure. If Africa's Talking is
        unavailable, the fallback (Twilio) adapter could be used — but for
        an alert about *delivery failures*, we deliberately use the primary
        provider: if the primary is down, the alert itself may fail, which
        is acceptable (the connectivity watcher raises a separate alarm).

        Args:
            business_name: name of the business (for the alert message body)
            phone: admin phone number in E.164 format (e.g., +254712345678)
            failure_rate: the computed failure rate (0.0–1.0)
        """
        # Import here (not at module top) so the adapter is only initialised
        # when an alert is actually sent — avoids requiring AT credentials
        # during import / unit tests that never send.
        from adapters.africas_talking import get_africas_talking_adapter

        pct = failure_rate * 100
        body = (
            f"ALERT: {business_name} SMS failure rate is {pct:.1f}% "
            f"in the last hour. Please investigate."
        )

        adapter = get_africas_talking_adapter()
        await adapter.send(
            to=phone,
            body=body,
            # client_message_id makes this idempotent — if the alert is
            # retried within the same minute, the outbox dedup catches it.
            client_message_id=f"alert:{business_name}:{datetime.utcnow().strftime('%Y%m%d%H%M')}",
        )

    async def run_alert_check(self, business_id: int) -> dict:
        """
        Full alert check + send cycle. Suitable for a Celery task.

        1. Opens a session and checks the failure rate
        2. If should_alert, loads the business name and admin phone
        3. Sends an SMS alert to the admin
        4. Logs an audit event
        5. Returns a summary dict

        The admin phone is read from the ALERT_ADMIN_PHONE env var.
        If not set, the alert is skipped and the result notes the missing config.

        Args:
            business_id: business to check (was school_id in tuition agent)

        Returns:
            dict with keys: business_id, should_alert, failure_rate,
            total_sent, total_failed, alert_sent (bool), error (str|None)
        """
        result = {
            "business_id": business_id,
            "should_alert": False,
            "failure_rate": 0.0,
            "total_sent": 0,
            "total_failed": 0,
            "alert_sent": False,
            "error": None,
        }

        async with async_session_factory() as session:
            try:
                # 1. Check failure rate
                alert_result = await self.check_failure_rate(session, business_id)

                result["should_alert"] = alert_result.should_alert
                result["failure_rate"] = alert_result.failure_rate
                result["total_sent"] = alert_result.total_sent
                result["total_failed"] = alert_result.total_failed

                if not alert_result.should_alert:
                    return result

                # 2. Load business name (was School in the tuition agent)
                business_result = await session.execute(
                    select(Business).where(Business.id == business_id)
                )
                business = business_result.scalar_one_or_none()
                if not business:
                    result["error"] = f"Business {business_id} not found"
                    return result

                # 3. Get admin phone from env (per-business config would be better)
                admin_phone = os.environ.get("ALERT_ADMIN_PHONE")
                if not admin_phone:
                    result["error"] = "ALERT_ADMIN_PHONE env var not set — cannot send alert"
                    return result

                # 4. Send the alert via Africa's Talking (primary provider)
                await self.send_alert(
                    business_name=business.name,
                    phone=admin_phone,
                    failure_rate=alert_result.failure_rate,
                )
                result["alert_sent"] = True

                # 5. Audit log
                # NOTE: AuditContext.school_id is the legacy field name
                # defined in infra/audit_logger.py; it carries the business_id
                # in this codebase. We pass business_id into that field.
                await log_audit_event(
                    event_type=AuditEventType.MESSAGE_FAILED,
                    entity_type="business",
                    entity_id=str(business_id),
                    summary=(
                        f"Failure rate alert sent for {business.name}: "
                        f"{alert_result.failure_rate * 100:.1f}% "
                        f"({alert_result.total_failed}/{alert_result.total_sent} failed)"
                    ),
                    context=AuditContext(
                        school_id=business_id,  # legacy field name; carries business_id
                        actor_type="system",
                        actor_id="alert_service",
                    ),
                )

                await session.commit()

            except Exception as exc:
                await session.rollback()
                result["error"] = str(exc)

        return result