"""
workers/reminders.py — Celery task: compute reminder candidates daily
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
This is the task run by Celery Beat every morning at 6 AM EAT
(configurable). It:

  1. Loads the business and all active transactions (credit, layaway,
     service — not SALE, which is paid at the counter).
  2. Computes reminder candidates via ReminderService.build_candidates().
  3. Checks suppression rules (already paid, opted out, cancelled).
  4. Inserts into the outbox with ON CONFLICT DO NOTHING (idempotency).
  5. Logs audit events for each suppressed reminder.

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - School → Business, Student → Customer, Guardian → Contact,
    Invoice → Transaction.
  - Reminder types expanded: appointment reminders, credit follow-ups,
    layaway pickup reminders (in addition to the 14/3/today cadence).
  - SALE transactions are excluded (no reminders needed — paid at counter).
  - The reminder_service handles per-transaction-type logic.

INHERITED LOGIC (the 12-layer anti-duplicate foundation)
--------------------------------------------------------
  - Deterministic message_key (business:customer:contact:txn:type:date:v)
  - ON CONFLICT DO NOTHING on message_key → running scheduler twice is safe
  - Transactional outbox: scheduler + outbox write in ONE transaction
  - Suppression checks (paid, opted out) before insert

TEACHING NOTES
--------------
  - `@celery_app.task(bind=True, max_retries=3)` registers this as a
    Celery task that can retry on failure. `bind=True` gives access to
    `self` (the task instance) for retry calls.
  - We create a DB session inside the task because Celery workers run
    in separate processes from the FastAPI server — they need their own
    DB connections.
  - The entire operation is wrapped in a single transaction: if any part
    fails, nothing is committed (no partial sends).
  - `asyncio.run()` bridges Celery's synchronous task interface to our
    async SQLAlchemy code. Each task call creates a fresh event loop.

KENYA-SPECIFIC CONSIDERATIONS
-----------------------------
  - Runs at 6 AM EAT — before businesses open at 7 AM. Reminders are
    in the outbox, ready for the send worker to deliver at opening.
  - The send worker respects business hours (default 7–19), so even
    though reminders are computed at 6 AM, they're not sent until 7 AM.
  - For clinic/salon businesses (SERVICE type), the "due_date" is the
    APPOINTMENT date, not a payment deadline. The same cadence applies.
  - Late notices only apply to CREDIT and LAYAWAY (not SALE or SERVICE).
═══════════════════════════════════════════════════════════════════════
"""

from datetime import date

from celery import shared_task
from sqlalchemy import select

from domain.dispatch_service import DispatchService
from domain.models import (
    Business,
    Contact,
    OutboundMessage,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from domain.reminder_service import ReminderService
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from workers.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def compute_reminder_candidates(self, business_id: int = 1) -> dict:
    """
    Celery task: compute and insert reminder candidates for a business.

    Args:
        business_id: the business to process (default 1 for single-business
                      deployments; multi-tenant setups would iterate).

    Returns:
        {"processed": N, "inserted": M, "suppressed": K, "errors": [...]}
    """
    import asyncio

    return asyncio.run(_async_compute_reminder_candidates(business_id))


async def _async_compute_reminder_candidates(business_id: int) -> dict:
    """Async implementation of the scheduler task."""
    reminder_service = ReminderService()
    dispatch_service = DispatchService()

    # Result counters for monitoring + dashboard.
    result: dict = {
        "processed": 0,
        "inserted": 0,
        "suppressed": 0,
        "duplicates_skipped": 0,
        "errors": [],
    }

    async with async_session_factory() as session:
        try:
            # ── 1. Load the business ──────────────────────────────
            # The business record holds the timezone and reminder_policy
            # (JSON) that drive reminder scheduling.
            biz_result = await session.execute(
                select(Business).where(
                    Business.id == business_id,
                    Business.deleted_at.is_(None),
                )
            )
            business = biz_result.scalar_one_or_none()
            if not business:
                result["errors"].append(f"Business {business_id} not found")
                return result

            # ── 2. Load active transactions ───────────────────────
            # Active = not paid, not cancelled, not deleted.
            # We also exclude SALE type (paid at counter, no reminders).
            # SERVICE type IS included (appointment reminders).
            txn_result = await session.execute(
                select(Transaction).where(
                    Transaction.business_id == business_id,
                    Transaction.status.in_([
                        TransactionStatus.PENDING,
                        TransactionStatus.PARTIAL,
                        TransactionStatus.OVERDUE,
                    ]),
                    # Exclude SALE transactions — they're paid immediately.
                    # CREDIT, LAYAWAY, and SERVICE all need reminders.
                    Transaction.type != TransactionType.SALE,
                    Transaction.deleted_at.is_(None),
                )
            )
            transactions = list(txn_result.scalars().all())
            result["processed"] = len(transactions)

            if not transactions:
                return result  # nothing to do today

            # ── 3. Build candidates via ReminderService ──────────
            # This is pure business logic — no DB writes. It computes
            # which reminder types are due today for each transaction.
            today = date.today()
            candidates = reminder_service.build_candidates(
                business, transactions, today=today
            )

            # ── 4. Apply suppression checks per candidate ──────────
            # Suppression rules (inherited from tuition agent):
            #   - Transaction fully paid → suppress
            #   - Contact opted out → suppress (Kenya DPA 2019)
            #   - Transaction cancelled → suppress
            final_candidates = []
            for candidate in candidates:
                # Load the contact for opt-in check.
                contact_result = await session.execute(
                    select(Contact).where(Contact.id == candidate.contact_id)
                )
                contact = contact_result.scalar_one_or_none()
                if not contact:
                    continue

                # Find the corresponding transaction for the suppression check.
                transaction = next(
                    (t for t in transactions if t.id == candidate.transaction_id),
                    None,
                )
                if not transaction:
                    continue

                suppressed, reason = reminder_service.should_suppress(
                    transaction, contact
                )

                if suppressed:
                    result["suppressed"] += 1
                    await log_audit_event(
                        event_type="reminder.suppressed",
                        entity_type="message",
                        entity_id=candidate.message_key,
                        summary=f"Reminder suppressed: {reason}",
                        context=AuditContext(
                            business_id=business_id,
                            actor_type="scheduler",
                        ),
                    )
                    continue

                final_candidates.append(candidate)

            # ── 5. Insert into outbox (transactional, idempotent) ──
            # DispatchService.bulk insert uses ON CONFLICT DO NOTHING
            # on message_key. Running the scheduler twice is safe —
            # duplicate candidates are silently ignored by the DB.
            dispatch_result = await dispatch_service.insert_outbox_messages(
                session=session,
                candidates=final_candidates,
            )
            result["inserted"] = dispatch_result["inserted"]
            result["duplicates_skipped"] = dispatch_result["duplicates_skipped"]

            # ── 6. Commit everything in one transaction ────────────
            # If anything fails, NOTHING is committed — no partial sends,
            # no orphaned outbox entries. This is the transactional
            # outbox pattern's core guarantee.
            await session.commit()

        except Exception as exc:
            await session.rollback()
            result["errors"].append(str(exc))
            raise

    return result