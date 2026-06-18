"""
domain/reminder_service.py — Reminder eligibility + message key generation (business)
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
This service decides WHO gets WHAT reminder WHEN. It does NOT send
messages — it only builds the list of intended reminders and returns
them as `ReminderCandidate` dataclasses. The scheduler then inserts
candidates into the outbox via `DispatchService` (transactional, Step 10).

ADAPTATIONS FROM THE TUITION AGENT
---------------------------------
  - "School" → "Business", "Student" → "Customer",
    "Guardian" → "Contact", "Invoice" → "Transaction".
  - Reminder types expanded: appointment reminders, credit follow-ups,
    layaway pickup reminders, promos, loyalty updates.
  - Due date may be None for SALE transactions (paid immediately,
    no reminders needed).
  - Date math uses the business's timezone (default Africa/Nairobi).

CORE LOGIC (inherited)
----------------------
  1. Load all active transactions for a business.
  2. For each transaction, determine which reminder types are due today.
  3. Check suppression rules (already paid, opted out, max attempts).
  4. Build deterministic message_key for deduplication.
  5. Return a list of OutboundMessage candidates.

TEACHING NOTES
--------------
  - "Eligibility" is pure business logic — no side effects (no DB writes).
    The scheduler calls this, then writes to the outbox.
  - `message_key` is computed deterministically so duplicate runs of
    the scheduler produce identical keys, which the DB rejects.
  - All date math uses the business's timezone (from `business.timezone`).
  - `is_late_notice_eligible` handles CREDIT/LAYAWAY types only — a SALE
    is never "late" because it was paid at the counter.
  - For SERVICE type (appointments), the reminder cadence is the same
    (14/3/today), but the "due_date" is the APPOINTMENT date, and there
    is no "late notice" (a missed appointment doesn't accrue a balance).

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - Scheduler (workers/reminders.py) calls `build_candidates()`.
  - `DispatchService` (domain/dispatch_service.py) inserts the returned
    candidates into `outbound_messages` with ON CONFLICT DO NOTHING.
  - `domain/templates.py` provides the actual message body via the
    template name returned in `body_template`.
  - `domain/policy_service.py` supplies the schedule dict and quiet hours.
═══════════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from zoneinfo import ZoneInfo

from domain.models import (
    Business,
    Contact,
    Customer,
    MessageStatus,
    OutboundMessage,
    ReminderType,
    Transaction,
    TransactionStatus,
    TransactionType,
)


@dataclass(frozen=True)
class ReminderCandidate:
    """
    A single reminder that SHOULD be sent (before suppression checks).

    `frozen=True` makes the dataclass immutable and hashable — safe to
    use in sets for deduplication in memory before hitting the DB.
    """
    business_id: int
    transaction_id: int
    customer_id: int
    contact_id: int
    reminder_type: ReminderType
    due_date: Optional[date]      # None for SALE-type (no due date)
    message_key: str
    body_template: str           # template name (looked up in templates.py)
    language: str = "en"         # customer's preferred language (en / sw)


class ReminderService:
    """
    Computes reminder candidates and suppression rules.

    Stateless: safe to instantiate once at module level or per request.
    """

    # Default reminder schedule (days before due/appointment date).
    # These mirror the tuition agent exactly — the cadence (14/3/today)
    # is proven to work and carries over unchanged.
    DEFAULT_SCHEDULE: dict[ReminderType, int] = {
        ReminderType.DUE_14: 14,
        ReminderType.DUE_3: 3,
        ReminderType.DUE_TODAY: 0,
    }

    # Reminder types that apply per transaction type. This is NEW:
    # SALE transactions don't get reminders; SERVICE gets appointment
    # reminders; CREDIT/LAYAWAY get the full reminder cadence.
    REMINDER_TYPES_BY_TXN: dict[TransactionType, set[ReminderType]] = {
        TransactionType.SALE: set(),  # paid at counter, no reminders
        TransactionType.CREDIT: {
            ReminderType.DUE_14,
            ReminderType.DUE_3,
            ReminderType.DUE_TODAY,
            ReminderType.LATE_NOTICE,
        },
        TransactionType.LAYAWAY: {
            ReminderType.DUE_14,
            ReminderType.DUE_3,
            ReminderType.DUE_TODAY,
            ReminderType.LATE_NOTICE,
            ReminderType.LAYAWAY_PICKUP,
        },
        TransactionType.SERVICE: {
            ReminderType.DUE_14,       # appointment in 14 days
            ReminderType.DUE_3,        # appointment in 3 days
            ReminderType.DUE_TODAY,    # appointment is today
            ReminderType.APPOINTMENT_REMINDER,
        },
    }

    def compute_message_key(
        self,
        business_id: int,
        customer_id: int,
        contact_id: int,
        transaction_id: int,
        reminder_type: ReminderType,
        due_date: Optional[date],
        policy_version: str = "v1",
    ) -> str:
        """
        Deterministic key for deduplication.

        Format: {business}:{customer}:{contact}:{txn}:{type}:{due_date}:{policy}
        Example: 1:101:201:1001:due_14:2026-05-15:v1

        If due_date is None (SALE type), we use "none" as a stable token
        so the key is still deterministic.

        TEACHING NOTE: This key is the foundation of the 12-layer
        anti-duplicate algorithm. Running the scheduler twice produces
        the same key, and the DB's UNIQUE constraint rejects the
        duplicate insert. See domain/outbox.py and dispatch_service.py.
        """
        due_str = due_date.isoformat() if due_date else "none"
        return (
            f"{business_id}:{customer_id}:{contact_id}:{transaction_id}:"
            f"{reminder_type.value}:{due_str}:{policy_version}"
        )

    def compute_reminder_date(
        self,
        due_date: date,
        reminder_type: ReminderType,
        schedule: Optional[dict[ReminderType, int]] = None,
    ) -> date:
        """
        Given a due/appointment date and reminder type, return the
        calendar date when that reminder should be sent.

        Examples (due_date = May 15):
          DUE_14    → May 1  (14 days before)
          DUE_3     → May 12 (3 days before)
          DUE_TODAY → May 15 (0 days before)
        """
        sched = schedule or self.DEFAULT_SCHEDULE
        days_before = sched.get(reminder_type, 0)
        return due_date - timedelta(days=days_before)

    def is_reminder_due_today(
        self,
        transaction: Transaction,
        reminder_type: ReminderType,
        today: date,
        schedule: Optional[dict[ReminderType, int]] = None,
    ) -> bool:
        """
        Check if a specific reminder type should trigger today.

        Returns False if the transaction has no due_date (SALE type).
        """
        if transaction.due_date is None:
            return False
        reminder_date = self.compute_reminder_date(
            transaction.due_date, reminder_type, schedule
        )
        return reminder_date == today

    def is_late_notice_eligible(
        self,
        transaction: Transaction,
        today: date,
    ) -> bool:
        """
        Late notices trigger the day AFTER due date if still unpaid.

        INHERITED LOGIC: only applies to transactions that have a
        meaningful "overdue" state (CREDIT, LAYAWAY). SALE and SERVICE
        transactions don't accrue late fees.
        """
        if transaction.status in (TransactionStatus.PAID, TransactionStatus.CANCELLED):
            return False
        if transaction.type not in (TransactionType.CREDIT, TransactionType.LAYAWAY):
            return False
        if transaction.due_date is None:
            return False
        return transaction.due_date < today

    def should_suppress(
        self,
        transaction: Transaction,
        contact: Contact,
    ) -> tuple[bool, Optional[str]]:
        """
        Check if a reminder should be suppressed. Returns (suppressed, reason).

        INHERITED suppression rules:
          1. Transaction fully paid → suppress
          2. Contact opted out → suppress (Kenya DPA 2019)
          3. Transaction cancelled → suppress

        Returns the reason string so the caller can record it in
        `outbound_messages.suppression_reason` for audit.
        """
        if transaction.status == TransactionStatus.PAID:
            return True, "transaction_paid"
        if transaction.status == TransactionStatus.CANCELLED:
            return True, "transaction_cancelled"
        if not contact.sms_opt_in:
            return True, "contact_opted_out"
        return False, None

    def build_candidates(
        self,
        business: Business,
        transactions: list[Transaction],
        today: Optional[date] = None,
        schedule: Optional[dict[ReminderType, int]] = None,
        policy_version: str = "v1",
    ) -> list[ReminderCandidate]:
        """
        Build all reminder candidates for a business on a given day.

        MAIN ENTRY POINT — called by the daily scheduler (workers/reminders.py).

        Returns only candidates that are due today and not yet suppressed.
        Suppression (opt-out, paid) is checked by the caller before insert,
        OR by the `should_suppress` method — this method focuses on
        "what's due" and leaves "what's allowed" to the caller for
        flexibility (e.g., the caller may want to log suppression reasons).
        """
        today = today or date.today()
        candidates: list[ReminderCandidate] = []

        for transaction in transactions:
            # Skip terminal states early (saves work).
            if transaction.status in (TransactionStatus.PAID, TransactionStatus.CANCELLED):
                continue

            # Determine which reminder types are eligible for this txn type.
            eligible_types = self.REMINDER_TYPES_BY_TXN.get(transaction.type, set())

            # Check scheduled reminders (DUE_14, DUE_3, DUE_TODAY).
            due_types: list[ReminderType] = []
            for rtype in (ReminderType.DUE_14, ReminderType.DUE_3, ReminderType.DUE_TODAY):
                if rtype in eligible_types and self.is_reminder_due_today(
                    transaction, rtype, today, schedule
                ):
                    due_types.append(rtype)

            # Late notice (separate logic — see is_late_notice_eligible).
            if ReminderType.LATE_NOTICE in eligible_types:
                if self.is_late_notice_eligible(transaction, today):
                    due_types.append(ReminderType.LATE_NOTICE)

            # Layaway pickup: when layaway is fully paid, remind to pick up.
            if ReminderType.LAYAWAY_PICKUP in eligible_types:
                if transaction.status == TransactionStatus.PAID:
                    due_types.append(ReminderType.LAYAWAY_PICKUP)

            # Build candidate for each due type.
            for rtype in due_types:
                key = self.compute_message_key(
                    business_id=business.id,
                    customer_id=transaction.customer_id,
                    contact_id=transaction.contact_id,
                    transaction_id=transaction.id,
                    reminder_type=rtype,
                    due_date=transaction.due_date,
                    policy_version=policy_version,
                )
                candidates.append(ReminderCandidate(
                    business_id=business.id,
                    transaction_id=transaction.id,
                    customer_id=transaction.customer_id,
                    contact_id=transaction.contact_id,
                    reminder_type=rtype,
                    due_date=transaction.due_date,
                    message_key=key,
                    body_template=self._get_template_name(rtype),
                ))

        return candidates

    def _get_template_name(self, reminder_type: ReminderType) -> str:
        """
        Map a ReminderType to the template name in domain/templates.py.

        TEACHING NOTE: This indirection lets us change template names
        without touching the reminder logic. The templates.py file
        holds the actual message bodies (EN + SW variants).
        """
        template_map = {
            ReminderType.DUE_14: "reminder_due_14",
            ReminderType.DUE_3: "reminder_due_3",
            ReminderType.DUE_TODAY: "reminder_due_today",
            ReminderType.LATE_NOTICE: "reminder_late",
            ReminderType.PAYMENT_CONFIRMED: "payment_confirmed",
            ReminderType.CALLBACK_ACK: "callback_ack",
            ReminderType.CREDIT_TERMS_ACK: "credit_terms_ack",
            ReminderType.APPOINTMENT_REMINDER: "book_appointment",
            ReminderType.LAYAWAY_PICKUP: "reminder_due_today",
            ReminderType.PROMO: "promo_message",
            ReminderType.LOYALTY_POINTS: "loyalty_points",
        }
        return template_map.get(reminder_type, "generic")