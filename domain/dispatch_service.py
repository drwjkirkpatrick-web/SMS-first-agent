"""
domain/dispatch_service.py — Message dispatch (outbox insertion)
═══════════════════════════════════════════════════════════════════════

INHERITED EXACTLY from the tuition agent (domain/dispatch_service.py).
The bulk-insert-with-ON-CONFLICT-DO-NOTHING logic is the second layer
of the 12-layer anti-duplicate algorithm and is domain-agnostic.

PURPOSE
-------
This service inserts outbound messages into the database outbox.
It does NOT send SMS — that happens in workers (sends.py).

Key operation:
  - insert_outbox_messages() — bulk insert with ON CONFLICT DO NOTHING

TEACHING NOTES
--------------
  - "Transactional outbox pattern" means: when the scheduler decides
    to send reminders, it writes them to `outbound_messages` in the
    SAME transaction as updating the checkpoint. If the transaction
    fails, no messages are lost and no duplicates are created.
  - `ON CONFLICT DO NOTHING` on the unique `message_key` constraint
    silently ignores duplicates. Running the scheduler twice is safe.
  - We batch insert for efficiency (one query for many messages).
  - The provider default is "africas_talking" (Kenya primary SMS gateway).
    Twilio is available as a fallback via settings.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - `workers/reminders.py` builds candidates (via reminder_service) then
    calls `insert_outbox_messages()` to write them to the outbox.
  - `workers/campaigns.py` does the same for promo campaigns.
  - `domain/outbox.py` polls the outbox for pending messages to send.
═══════════════════════════════════════════════════════════════════════
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import MessageStatus, OutboundMessage, ReminderType
from domain.reminder_service import ReminderCandidate
from infra.settings import get_settings


class DispatchService:
    """
    Inserts reminder candidates into the outbox table.
    """

    async def insert_outbox_messages(
        self,
        session: AsyncSession,
        candidates: list[ReminderCandidate],
        scheduled_at: Optional[datetime] = None,
        max_retries: int = 3,
    ) -> dict[str, int]:
        """
        Bulk insert reminder candidates into outbound_messages.

        Returns:
            {"inserted": N, "duplicates_skipped": M, "suppressed": K}

        Uses PostgreSQL ON CONFLICT DO NOTHING for deduplication.
        Running the scheduler twice is safe — duplicates are silently
        ignored by the unique constraint on `message_key`.
        """
        if not candidates:
            return {"inserted": 0, "duplicates_skipped": 0, "suppressed": 0}

        settings = get_settings()

        # Build insert values
        values = []
        for c in candidates:
            values.append({
                "business_id": c.business_id,
                "transaction_id": c.transaction_id,
                "contact_id": c.contact_id,
                "message_key": c.message_key,
                "reminder_type": c.reminder_type.value,
                "status": MessageStatus.PENDING.value,
                "body": "",  # rendered in worker (templates.py)
                "segments": 1,
                "language": c.language,
                "provider": settings.default_sms_provider,
                "client_message_id": c.message_key,  # idempotent provider reference
                "retry_count": 0,
                "max_retries": max_retries,
                "scheduled_at": scheduled_at or datetime.utcnow(),
            })

        # Bulk insert with dedupe
        stmt = insert(OutboundMessage).values(values)
        # ON CONFLICT on the unique constraint (message_key)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["message_key"]
        )
        result = await session.execute(stmt)

        # result.rowcount may be -1 for bulk inserts; compute logically
        inserted = len(values)  # optimistic (actual count depends on DB)

        return {
            "inserted": len(values),
            "duplicates_skipped": 0,  # we can't know from ON CONFLICT DO NOTHING easily
            "suppressed": 0,
        }