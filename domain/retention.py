"""
domain/retention.py — Data retention purge service
═══════════════════════════════════════════════════

Periodically purges old message data to control database growth and
comply with data retention policies.

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - School → Business, school_id → business_id, schools → businesses
  - Guardian → Contact (this module doesn't reference contact_id
    directly, but audit summaries use business-aware language).
  - Monetary / cost references are in KES (not USD). This module
    doesn't deal with amounts, but the docstring + audit wording
    avoids USD phrasing.
  - Default timezone context is Africa/Nairobi (not
    America/Los_Angeles). The purge uses utcnow() for cutoffs, which
    is timezone-agnostic; the Africa/Nairobi context matters for the
    Celery beat schedule (daily at 03:00 EAT), configured elsewhere.
  - Audit event type: the tuition agent reused `AuditEventType.SIS_SYNC`
    for retention purges. The SMS-first-agent AuditEventType enum (see
    domain/models.py) does NOT define SIS_SYNC — the closest general
    "system data-handling" event is `POLICY_CHANGED`. We use that,
    with entity_type="retention" so the audit log is unambiguous.
  - AuditContext keeps the legacy field name `school_id` (defined in
    infra/audit_logger.py) but it carries the business_id in practice;
    we pass business_id into that field.

Retention periods (defaults, configurable per call):
  - Sent/delivered messages: 90 days (soft delete via deleted_at)
  - Failed messages:         30 days (soft delete via deleted_at)
  - Delivery callbacks:      30 days (hard delete — ephemeral webhook data)
  - Inbound messages:        30 days (hard delete — see note below)

SOFT DELETE VS HARD DELETE
-------------------------
  - OutboundMessage does NOT have a deleted_at column in the current
    schema, so purge_sent_messages and purge_failed_messages use hard
    DELETE. If a deleted_at column is added in a future migration,
    switch these to UPDATE SET deleted_at = now() for audit-trail
    preservation (Kenya DPA 2019 favours soft deletes for PII-bearing
    records).
  - DeliveryCallback and InboundMessage are always hard-deleted — they
    are ephemeral operational data with no long-term audit value.

TEACHING NOTES
--------------
  - Purges run in their own transaction via async_session_factory().
  - Each purge method returns the number of rows affected.
  - run_retention_purge() is suitable as a Celery beat task (daily).
  - The business_id parameter in run_retention_purge is optional — if
    provided, purges are scoped to that business; if None, purges run
    across all businesses.
  - Kenya DPA 2019: retention purges are themselves auditable data-
    handling events. We log every purge that removes > 0 rows so the
    ODPC (Office of the Data Protection Commissioner) can inspect
    when and how much data was disposed of.
═══════════════════════════════════════════════════
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import (
    AuditEventType,
    DeliveryCallback,
    InboundMessage,
    MessageStatus,
    OutboundMessage,
)
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory


class RetentionService:
    """
    Purges old message data according to retention policies.

    Each purge method accepts an AsyncSession (for transactional use)
    and returns the count of affected rows. run_retention_purge()
    manages its own session and is safe to call from Celery.
    """

    async def purge_sent_messages(
        self,
        session: AsyncSession,
        days: int = 90,
        business_id: Optional[int] = None,
    ) -> int:
        """
        Delete sent/delivered messages older than N days.

        Uses hard delete because OutboundMessage has no deleted_at
        column in the current schema. If soft-delete is added later,
        replace the DELETE with UPDATE ... SET deleted_at = now().

        Args:
            session: active async DB session
            days: retention period in days (default 90)
            business_id: optional business filter (was school_id)

        Returns:
            Number of rows deleted
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        terminal_statuses = [MessageStatus.SENT, MessageStatus.DELIVERED]

        stmt = (
            delete(OutboundMessage)
            .where(
                OutboundMessage.status.in_(terminal_statuses),
                OutboundMessage.updated_at < cutoff,
            )
        )
        if business_id is not None:
            stmt = stmt.where(OutboundMessage.business_id == business_id)

        result = await session.execute(stmt)
        return result.rowcount

    async def purge_failed_messages(
        self,
        session: AsyncSession,
        days: int = 30,
        business_id: Optional[int] = None,
    ) -> int:
        """
        Delete failed messages older than N days.

        Uses hard delete (OutboundMessage has no deleted_at column).

        Args:
            session: active async DB session
            days: retention period in days (default 30)
            business_id: optional business filter (was school_id)

        Returns:
            Number of rows deleted
        """
        cutoff = datetime.utcnow() - timedelta(days=days)

        stmt = (
            delete(OutboundMessage)
            .where(
                OutboundMessage.status == MessageStatus.FAILED,
                OutboundMessage.updated_at < cutoff,
            )
        )
        if business_id is not None:
            stmt = stmt.where(OutboundMessage.business_id == business_id)

        result = await session.execute(stmt)
        return result.rowcount

    async def purge_delivery_callbacks(
        self,
        session: AsyncSession,
        days: int = 30,
        business_id: Optional[int] = None,
    ) -> int:
        """
        Hard-delete delivery callback records older than N days.

        Delivery callbacks are ephemeral webhook payloads with no
        long-term audit value — hard delete is appropriate.

        Args:
            session: active async DB session
            days: retention period in days (default 30)
            business_id: optional business filter (joins via outbound_messages)

        Returns:
            Number of rows deleted
        """
        cutoff = datetime.utcnow() - timedelta(days=days)

        if business_id is not None:
            # Filter by business via subquery on outbound_messages
            business_msg_ids = (
                select(OutboundMessage.id)
                .where(OutboundMessage.business_id == business_id)
                .scalar_subquery()
            )
            stmt = (
                delete(DeliveryCallback)
                .where(
                    DeliveryCallback.message_id.in_(business_msg_ids),
                    DeliveryCallback.created_at < cutoff,
                )
            )
        else:
            stmt = delete(DeliveryCallback).where(
                DeliveryCallback.created_at < cutoff
            )

        result = await session.execute(stmt)
        return result.rowcount

    async def purge_inbound_messages(
        self,
        session: AsyncSession,
        days: int = 30,
        business_id: Optional[int] = None,
    ) -> int:
        """
        Hard-delete inbound messages older than N days.

        Inbound messages are customer texts that have been processed
        (intent classified, payments / credit-terms requests extracted).
        After processing, the raw message body has no long-term value.

        Args:
            session: active async DB session
            days: retention period in days (default 30)
            business_id: optional business filter (was school_id)

        Returns:
            Number of rows deleted
        """
        cutoff = datetime.utcnow() - timedelta(days=days)

        stmt = delete(InboundMessage).where(
            InboundMessage.created_at < cutoff
        )
        if business_id is not None:
            stmt = stmt.where(InboundMessage.business_id == business_id)

        result = await session.execute(stmt)
        return result.rowcount

    async def run_retention_purge(
        self,
        business_id: Optional[int] = None,
    ) -> dict:
        """
        Run all retention purges in a single transaction.

        Suitable as a Celery beat task (e.g., daily at 03:00 EAT).
        Returns a summary dict with counts for each purge type.

        Args:
            business_id: if provided, scope purges to this business;
                         if None, purge across all businesses

        Returns:
            dict with keys: sent_purged, failed_purged,
            callbacks_purged, inbound_purged, total_purged,
            business_id, error
        """
        result = {
            "sent_purged": 0,
            "failed_purged": 0,
            "callbacks_purged": 0,
            "inbound_purged": 0,
            "total_purged": 0,
            "business_id": business_id,
            "error": None,
        }

        async with async_session_factory() as session:
            try:
                result["sent_purged"] = await self.purge_sent_messages(
                    session, days=90, business_id=business_id,
                )
                result["failed_purged"] = await self.purge_failed_messages(
                    session, days=30, business_id=business_id,
                )
                result["callbacks_purged"] = await self.purge_delivery_callbacks(
                    session, days=30, business_id=business_id,
                )
                result["inbound_purged"] = await self.purge_inbound_messages(
                    session, days=30, business_id=business_id,
                )

                result["total_purged"] = (
                    result["sent_purged"]
                    + result["failed_purged"]
                    + result["callbacks_purged"]
                    + result["inbound_purged"]
                )

                # Audit log the purge operation.
                # The tuition agent reused AuditEventType.SIS_SYNC for this,
                # but the SMS-first-agent AuditEventType enum has no SIS_SYNC.
                # POLICY_CHANGED is the closest general "data-handling" event;
                # entity_type="retention" keeps the audit entry unambiguous.
                if result["total_purged"] > 0:
                    await log_audit_event(
                        event_type=AuditEventType.POLICY_CHANGED,
                        entity_type="retention",
                        entity_id=str(business_id) if business_id else "all",
                        summary=(
                            f"Retention purge: {result['total_purged']} rows removed "
                            f"(sent={result['sent_purged']}, failed={result['failed_purged']}, "
                            f"callbacks={result['callbacks_purged']}, inbound={result['inbound_purged']})"
                        ),
                        context=AuditContext(
                            school_id=business_id,  # legacy field name; carries business_id
                            actor_type="system",
                            actor_id="retention_service",
                        ),
                    )

                await session.commit()

            except Exception as exc:
                await session.rollback()
                result["error"] = str(exc)

        return result