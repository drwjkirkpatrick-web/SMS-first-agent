"""
domain/dead_letter.py — Dead letter queue for permanently failed messages
═══════════════════════════════════════════════════

When a message exhausts its retry budget (retry_count >= max_retries) or
suffers a non-retryable failure, it is moved to the dead letter queue
instead of being silently discarded. This preserves the message content
and failure context for manual investigation or replay.

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - School → Business, school_id → business_id, schools → businesses
  - Guardian → Contact, guardian_id → contact_id, guardians → contacts
  - The dead letter ORM table now FKs to `businesses.id` and
    `contacts.id` (was schools.id / guardians.id).
  - OutboundMessage in this codebase has `business_id` and `contact_id`
    columns (NOT school_id / guardian_id), so the move + replay
    methods read those fields directly.
  - AuditContext keeps the legacy field name `school_id` (defined in
    infra/audit_logger.py) but it carries the business_id in practice;
    we pass business_id into that field.
  - Monetary / cost references are in KES; default timezone context is
    Africa/Nairobi. (This module doesn't deal with money or timezones
    directly, but the audit summaries avoid USD/LA phrasing.)

Flow:
  OutboundMessage (FAILED, retries exhausted)
    → DeadLetterService.move_to_dead_letter()
    → DeadLetterMessage record created
    → Original message can be archived or deleted

Replay:
  DeadLetterService.replay_from_dead_letter()
    → New OutboundMessage created with PENDING status
    → Dead letter record removed (or marked replayed)

TEACHING NOTES
--------------
  - The dead letter table is separate from outbound_messages so that
    retention purges on the outbox don't lose dead-lettered messages.
    A retention purge that deletes old sent/failed rows must NOT
    delete dead-lettered rows — they're held for investigation.
  - We store the full message body and metadata so operators can
    inspect what failed without joining back to the original table
    (which may have been purged).
  - The ORM model is defined here (not in models.py) to keep the
    dead-letter concern self-contained. It registers with Base.metadata
    on import, so init_db() will create the table as long as this
    module is imported at startup (e.g., in domain/__init__.py).
  - The dataclass `DeadLetterMessage` is the public return type — a
    frozen, detached snapshot with no ORM session lifecycle, safe to
    serialise in Celery task results / API responses.
═══════════════════════════════════════════════════
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    delete,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import (
    AuditEventType,
    MessageStatus,
    OutboundMessage,
    ReminderType,
)
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import Base, async_session_factory


# ═══════════════════════════════════════════════════
# ORM Model — dead_letter_messages table
# ═══════════════════════════════════════════════════

class DeadLetterMessageORM(Base):
    """
    Persistence model for dead-lettered messages.

    This table holds messages that have exhausted retries or suffered
    permanent failures. It is separate from outbound_messages so that
    outbox retention purges do not lose dead-lettered content.

    FK targets in this codebase:
      - business_id → businesses.id   (was schools.id)
      - contact_id  → contacts.id      (was guardians.id)
    """
    __tablename__ = "dead_letter_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    original_message_id: Mapped[int] = mapped_column(
        ForeignKey("outbound_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # was school_id → schools.id ; now business_id → businesses.id
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # was guardian_id → guardians.id ; now contact_id → contacts.id
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_key: Mapped[str] = mapped_column(String(255), nullable=False)
    reminder_type: Mapped[ReminderType] = mapped_column(
        Enum(ReminderType, name="reminder_type"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False)

    original_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    dead_lettered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        # Index for "list dead letters for a business, most recent first"
        Index("ix_dead_letter_business_created", "business_id", "dead_lettered_at"),
    )


# ═══════════════════════════════════════════════════
# Dataclass — public return type
# ═══════════════════════════════════════════════════

@dataclass(frozen=True)
class DeadLetterMessage:
    """
    Immutable snapshot of a dead-lettered message.

    Returned by DeadLetterService methods. This is a plain dataclass
    (not an ORM model) so callers receive a detached, serialisable
    object with no session lifecycle attached.

    Field naming mirrors the adapted domain: business_id (was school_id),
    contact_id (was guardian_id).
    """
    id: int
    original_message_id: int
    business_id: int
    contact_id: int
    message_key: str
    reminder_type: ReminderType
    body: str
    failure_reason: str
    original_created_at: datetime
    dead_lettered_at: datetime


def _orm_to_dataclass(row: DeadLetterMessageORM) -> DeadLetterMessage:
    """Convert an ORM row to the frozen dataclass return type."""
    return DeadLetterMessage(
        id=row.id,
        original_message_id=row.original_message_id,
        business_id=row.business_id,
        contact_id=row.contact_id,
        message_key=row.message_key,
        reminder_type=row.reminder_type,
        body=row.body,
        failure_reason=row.failure_reason,
        original_created_at=row.original_created_at,
        dead_lettered_at=row.dead_lettered_at,
    )


# ═══════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════

class DeadLetterService:
    """
    Manages the dead letter queue for failed outbound messages.

    All methods accept an AsyncSession so they can participate in the
    caller's transaction. Use async_session_factory() for standalone calls.
    """

    async def move_to_dead_letter(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        reason: str,
    ) -> DeadLetterMessage:
        """
        Create a dead letter record from a failed outbound message.

        Copies all relevant fields from the original message so the
        dead letter is self-contained (survives even if the original
        outbound_messages row is later purged by the retention service).

        Args:
            session: active async DB session
            message: the failed OutboundMessage to dead-letter
            reason: human-readable failure reason (provider error,
                    exhausted retries, non-retryable failure, etc.)

        Returns:
            DeadLetterMessage dataclass snapshot of the created record
        """
        row = DeadLetterMessageORM(
            original_message_id=message.id,
            business_id=message.business_id,   # was message.school_id
            contact_id=message.contact_id,     # was message.guardian_id
            message_key=message.message_key,
            reminder_type=message.reminder_type,
            body=message.body,
            failure_reason=reason,
            original_created_at=message.created_at,
            dead_lettered_at=datetime.utcnow(),
        )
        session.add(row)
        await session.flush()  # populate row.id

        # Audit the dead-lettering
        # AuditContext.school_id is the legacy field name; carries business_id.
        await log_audit_event(
            event_type=AuditEventType.MESSAGE_FAILED,
            entity_type="dead_letter",
            entity_id=str(row.id),
            summary=f"Message {message.message_key} moved to dead letter: {reason}",
            context=AuditContext(
                school_id=message.business_id,  # legacy field name; carries business_id
                actor_type="system",
                actor_id="dead_letter_service",
            ),
        )

        return _orm_to_dataclass(row)

    async def list_dead_letters(
        self,
        session: AsyncSession,
        business_id: int,
        limit: int = 50,
    ) -> list[DeadLetterMessage]:
        """
        List dead-lettered messages for a business, most recent first.

        Args:
            session: active async DB session
            business_id: business to filter by (was school_id)
            limit: maximum number of records to return (default 50)

        Returns:
            List of DeadLetterMessage dataclass snapshots
        """
        stmt = (
            select(DeadLetterMessageORM)
            .where(DeadLetterMessageORM.business_id == business_id)
            .order_by(DeadLetterMessageORM.dead_lettered_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return [_orm_to_dataclass(r) for r in rows]

    async def replay_from_dead_letter(
        self,
        session: AsyncSession,
        dead_letter_id: int,
    ) -> OutboundMessage:
        """
        Move a dead-lettered message back to the outbox for retry.

        Creates a new OutboundMessage with PENDING status and a fresh
        retry budget, then removes the dead letter record. The new
        message gets a unique message_key suffix to avoid collision
        with the original (which may still exist in outbound_messages).

        Args:
            session: active async DB session
            dead_letter_id: id of the DeadLetterMessageORM to replay

        Returns:
            The newly created OutboundMessage (PENDING status, ready for the send worker)

        Raises:
            ValueError: if the dead letter record is not found
        """
        # 1. Load the dead letter record
        result = await session.execute(
            select(DeadLetterMessageORM).where(
                DeadLetterMessageORM.id == dead_letter_id
            )
        )
        dl = result.scalar_one_or_none()
        if dl is None:
            raise ValueError(f"Dead letter {dead_letter_id} not found")

        # 2. Create a new outbound message for retry
        #    Append replay suffix to message_key to avoid unique constraint violation
        #    (the original message_key may still be present in outbound_messages).
        replay_key = f"{dl.message_key}:replay:{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

        new_message = OutboundMessage(
            business_id=dl.business_id,   # was school_id
            contact_id=dl.contact_id,     # was guardian_id
            message_key=replay_key,
            reminder_type=dl.reminder_type,
            status=MessageStatus.PENDING,
            body=dl.body,
            retry_count=0,
            max_retries=3,
            scheduled_at=datetime.utcnow(),
        )
        session.add(new_message)
        await session.flush()

        # 3. Remove the dead letter record (replayed successfully)
        await session.execute(
            delete(DeadLetterMessageORM).where(
                DeadLetterMessageORM.id == dead_letter_id
            )
        )

        # 4. Audit the replay
        # AuditContext.school_id is the legacy field name; carries business_id.
        await log_audit_event(
            event_type=AuditEventType.MESSAGE_SEND_ATTEMPT,
            entity_type="dead_letter",
            entity_id=str(dead_letter_id),
            summary=f"Dead letter {dead_letter_id} replayed as new outbound message {new_message.id}",
            context=AuditContext(
                school_id=dl.business_id,  # legacy field name; carries business_id
                actor_type="system",
                actor_id="dead_letter_service",
            ),
        )

        return new_message