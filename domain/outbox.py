"""
domain/outbox.py — Transactional outbox polling and state transitions
═══════════════════════════════════════════════════════════════════════

INHERITED EXACTLY from the tuition agent (domain/outbox.py).
The 12-layer anti-duplicate algorithm is universal — it applies equally
to business reminders, promos, and payment follow-ups. No changes
needed beyond the module docstring.

PURPOSE
-------
The Transactional Outbox Pattern is the heart of duplicate prevention:

  ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
  │  Scheduler  │────▶│  Outbox     │────▶│   Worker    │
  │  (reminders)│     │  (DB table) │     │  (sends.py) │
  └─────────────┘     └─────────────┘     └─────────────┘
        │                    │                  │
        ▼                    ▼                  ▼
   Writes messages      Workers claim      Sends via SMS
   in SAME transaction  with row lock      provider

Key guarantee: The scheduler and outbox write happen in ONE transaction.
If the scheduler crashes after writing to outbox but before committing,
PostgreSQL rolls back — no messages are orphaned.

Worker behavior:
  1. SELECT ... FOR UPDATE SKIP LOCKED pending messages
  2. UPDATE status = 'sending' (claim)
  3. Call SMS provider (Africa's Talking or Twilio)
  4. UPDATE status = 'sent' or 'failed' or 'unknown_delivery'

TEACHING NOTES
--------------
  - `FOR UPDATE SKIP LOCKED` is PostgreSQL-specific magic. It means:
    "Lock these rows so no other worker can touch them, BUT if another
    worker already locked a row, skip it and move to the next one."
    This prevents worker A and worker B from picking up the same message.
  - We use `pool_size=5` in infra/database.py because each worker needs
    a dedicated connection for the duration of the lock.
  - The state machine prevents retries from `sent` (no duplicates).

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - `workers/sends.py` calls `poll_pending()` and `transition_status()`.
  - `workers/reconciliation.py` calls `get_unknown_deliveries()`.
  - `domain/dispatch_service.py` inserts into the same outbox table.
  - `domain/models.py` defines the OutboundMessage model + MessageStatus.
═══════════════════════════════════════════════════════════════════════
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import MessageStatus, OutboundMessage


class OutboxService:
    """
    Reads from the outbox and manages message lifecycle.
    """

    # Valid state transitions (strict state machine).
    # INHERITED EXACTLY — no changes. The state machine is domain-agnostic.
    VALID_TRANSITIONS: dict[MessageStatus, set[MessageStatus]] = {
        MessageStatus.PENDING: {MessageStatus.SENDING},
        MessageStatus.SENDING: {
            MessageStatus.SENT,
            MessageStatus.FAILED,
            MessageStatus.UNKNOWN_DELIVERY,
        },
        MessageStatus.SENT: set(),  # terminal — no retry
        MessageStatus.DELIVERED: set(),  # terminal
        MessageStatus.FAILED: {MessageStatus.PENDING},  # retry if budget remains
        MessageStatus.UNKNOWN_DELIVERY: {
            MessageStatus.SENT,      # provider confirmed later
            MessageStatus.FAILED,    # provider says it failed
            MessageStatus.PENDING,   # provider says not found, retry
        },
        MessageStatus.SUPPRESSED: set(),  # terminal
    }

    async def poll_pending(
        self,
        session: AsyncSession,
        batch_size: int = 100,
        max_segments: int = 2,
    ) -> list[OutboundMessage]:
        """
        Poll the outbox for pending messages with row locking.

        Returns messages that are:
          - status = 'pending'
          - scheduled_at is in the past (or NULL)
          - retry_count < max_retries

        Uses FOR UPDATE SKIP LOCKED for safe concurrent workers.
        """
        now = datetime.utcnow()

        stmt = (
            select(OutboundMessage)
            .where(
                OutboundMessage.status == MessageStatus.PENDING,
                OutboundMessage.scheduled_at <= now,
                OutboundMessage.retry_count < OutboundMessage.max_retries,
            )
            .order_by(OutboundMessage.scheduled_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )

        result = await session.execute(stmt)
        messages = list(result.scalars().all())
        return messages

    async def claim_for_sending(
        self,
        session: AsyncSession,
        message: OutboundMessage,
    ) -> bool:
        """
        Mark a message as 'sending' to claim it from the outbox.
        Returns True if successful, False if another worker claimed it.
        """
        # Double-check status hasn't changed (race condition safety)
        if message.status != MessageStatus.PENDING:
            return False

        message.status = MessageStatus.SENDING
        message.updated_at = datetime.utcnow()
        await session.flush()
        return True

    async def transition_status(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        new_status: MessageStatus,
        provider_message_id: Optional[str] = None,
        error_info: Optional[str] = None,
    ) -> None:
        """
        Transition a message to a new status with validation.

        Args:
            new_status: target status
            provider_message_id: provider message ID (if known)
            error_info: error message or code (for FAILED)
        """
        current = message.status
        allowed = self.VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition: {current.value} → {new_status.value}"
            )

        message.status = new_status
        message.updated_at = datetime.utcnow()

        if new_status == MessageStatus.SENT:
            message.sent_at = datetime.utcnow()
            if provider_message_id:
                message.provider_message_id = provider_message_id

        elif new_status == MessageStatus.DELIVERED:
            message.delivered_at = datetime.utcnow()

        elif new_status == MessageStatus.FAILED:
            message.failed_at = datetime.utcnow()

        elif new_status == MessageStatus.UNKNOWN_DELIVERY:
            # No timestamp — we wait for reconciliation
            pass

        elif new_status == MessageStatus.PENDING and current == MessageStatus.FAILED:
            # Retry: reset for another attempt
            message.retry_count += 1
            # Exponential backoff: 1 min, 5 min, 15 min
            backoff_minutes = [1, 5, 15][min(message.retry_count - 1, 2)]
            message.next_retry_at = datetime.utcnow() + timedelta(minutes=backoff_minutes)
            message.status = MessageStatus.PENDING  # back to outbox

        await session.flush()

    async def get_unknown_deliveries(
        self,
        session: AsyncSession,
        older_than_minutes: int = 10,
    ) -> list[OutboundMessage]:
        """
        Find messages in unknown_delivery state that need reconciliation.
        Called by the reconciliation worker.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=older_than_minutes)
        stmt = (
            select(OutboundMessage)
            .where(
                OutboundMessage.status == MessageStatus.UNKNOWN_DELIVERY,
                OutboundMessage.updated_at <= cutoff,
            )
            .order_by(OutboundMessage.updated_at)
            .limit(100)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def is_duplicate_key(
        self,
        session: AsyncSession,
        message_key: str,
    ) -> bool:
        """
        Check if a message key already exists in the outbox.
        Used by the scheduler for pre-insert validation.
        """
        result = await session.execute(
            select(OutboundMessage).where(OutboundMessage.message_key == message_key)
        )
        return result.scalar_one_or_none() is not None