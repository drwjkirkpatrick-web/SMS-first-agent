"""
tests/integration/test_duplicate_prevention.py — Anti-duplicate integration tests
═══════════════════════════════════════════════════

These tests verify the 12-layer anti-duplicate algorithm works end-to-end
with a real PostgreSQL database. They are the most important tests in the system.

Test scenarios:
  1. Scheduler runs twice → zero duplicate messages (ON CONFLICT DO NOTHING)
  2. Two workers race for same message → only one claims it (FOR UPDATE SKIP LOCKED)
  3. Webhook receives duplicate delivery receipt → only one recorded (UNIQUE constraint)

Requires: PostgreSQL (not SQLite — FOR UPDATE SKIP LOCKED is PG-specific).

═══════════════════════════════════════════════════
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from domain.models import (
    Business,
    Contact,
    Customer,
    CustomerContactLink,
    DeliveryCallback,
    Language,
    MessageStatus,
    OutboundMessage,
    ReminderType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from domain.outbox import OutboxService
from domain.reminder_service import ReminderService
from infra.database import get_session_factory


@pytest.mark.asyncio
class TestSchedulerDuplicatePrevention:
    """Layer 1-3: Scheduler side deduplication — deterministic keys + ON CONFLICT DO NOTHING."""

    async def test_scheduler_run_twice_produces_no_duplicates(self):
        """
        Running the scheduler twice on the same day should produce
        the same message keys, and ON CONFLICT DO NOTHING should ignore
        all but the first insert.
        """
        service = ReminderService()
        today = date(2024, 1, 15)

        factory = get_session_factory()
        async with factory() as session:
            # Create business
            business = Business(name="Test Shop", timezone="Africa/Nairobi")
            session.add(business)
            await session.flush()

            # Create customer — use Language enum value (lowercase "en")
            customer = Customer(
                business_id=business.id,
                first_name="Wanjiru",
                preferred_language=Language.EN,
                loyalty_points=0,
            )
            session.add(customer)
            await session.flush()

            # Create contact
            contact = Contact(
                business_id=business.id,
                first_name="Wanjiru",
                phone="+254****5678",
                sms_opt_in=True,
            )
            session.add(contact)
            await session.flush()

            # Link customer → contact
            link = CustomerContactLink(
                customer_id=customer.id,
                contact_id=contact.id,
                is_primary_contact=True,
            )
            session.add(link)
            await session.flush()

            # Create transaction (credit type, due in 14 days)
            txn = Transaction(
                business_id=business.id,
                customer_id=customer.id,
                contact_id=contact.id,
                transaction_number="CRED-001",
                type=TransactionType.CREDIT,
                amount_due=Decimal("1500.00"),
                amount_paid=Decimal("0.00"),
                due_date=today + timedelta(days=14),
                status=TransactionStatus.PENDING,
            )
            session.add(txn)
            await session.commit()

            # Build candidates twice — should produce identical keys
            candidates1 = service.build_candidates(business, [txn], today=today)
            candidates2 = service.build_candidates(business, [txn], today=today)

            assert len(candidates1) == len(candidates2)
            keys1 = [c.message_key for c in candidates1]
            keys2 = [c.message_key for c in candidates2]
            assert keys1 == keys2
            assert len(candidates1) > 0  # at least one reminder due

            # Insert first batch
            for c in candidates1:
                msg = OutboundMessage(
                    business_id=c.business_id,
                    contact_id=c.contact_id,
                    transaction_id=c.transaction_id,
                    message_key=c.message_key,
                    reminder_type=c.reminder_type,
                    status=MessageStatus.PENDING,
                    body="",
                    segments=1,
                    provider="africas_talking",
                    client_message_id=c.message_key,
                )
                session.add(msg)
            await session.commit()

            # Try inserting again (should be ignored by UNIQUE constraint)
            for c in candidates2:
                stmt = pg_insert(OutboundMessage).values(
                    business_id=c.business_id,
                    contact_id=c.contact_id,
                    transaction_id=c.transaction_id,
                    message_key=c.message_key,
                    reminder_type=c.reminder_type,
                    status=MessageStatus.PENDING,
                    body="",
                    segments=1,
                    provider="africas_talking",
                    client_message_id=c.message_key,
                )
                stmt = stmt.on_conflict_do_nothing(index_elements=["message_key"])
                await session.execute(stmt)
            await session.commit()

            # Verify only one message exists per key
            result = await session.execute(
                select(OutboundMessage).where(
                    OutboundMessage.message_key == keys1[0]
                )
            )
            messages = result.scalars().all()
            assert len(messages) == 1  # no duplicate!

            # Cleanup
            await session.delete(txn)
            await session.delete(contact)
            await session.delete(customer)
            await session.delete(business)
            await session.commit()


@pytest.mark.asyncio
class TestWorkerDuplicatePrevention:
    """Layer 5-6: Worker side deduplication — FOR UPDATE SKIP LOCKED."""

    async def test_skip_locked_prevents_double_claim(self):
        """
        Two workers calling poll_pending() should never get the same message.
        FOR UPDATE SKIP LOCKED ensures one worker locks, the other skips.
        """
        outbox = OutboxService()

        factory = get_session_factory()
        async with factory() as session1:
            # Create a business + contact for the FK
            business = Business(name="Test Shop 2", timezone="Africa/Nairobi")
            session1.add(business)
            await session1.flush()

            contact = Contact(
                business_id=business.id,
                first_name="Test",
                phone="+254****9999",
                sms_opt_in=True,
            )
            session1.add(contact)
            await session1.flush()

            # Insert a test message
            msg = OutboundMessage(
                business_id=business.id,
                contact_id=contact.id,
                message_key="test:skip:locked:001",
                reminder_type=ReminderType.DUE_14,
                status=MessageStatus.PENDING,
                body="Test message",
                segments=1,
                provider="africas_talking",
                client_message_id="test:skip:locked:001",
                scheduled_at=datetime.utcnow(),
            )
            session1.add(msg)
            await session1.commit()

            # Worker 1 polls and claims
            msgs1 = await outbox.poll_pending(session1, batch_size=10)
            assert len(msgs1) >= 1

            # Find our message
            our_msg = [m for m in msgs1 if m.message_key == "test:skip:locked:001"]
            assert len(our_msg) == 1

            claimed = await outbox.claim_for_sending(session1, our_msg[0])
            assert claimed is True
            await session1.commit()

        # Worker 2 in a new session — should NOT find the message (it's SENDING)
        async with factory() as session2:
            msgs2 = await outbox.poll_pending(session2, batch_size=10)
            pending_msgs = [m for m in msgs2 if m.message_key == "test:skip:locked:001"]
            assert len(pending_msgs) == 0  # not available anymore

            # Cleanup
            from sqlalchemy import delete
            await session2.execute(delete(OutboundMessage).where(
                OutboundMessage.message_key == "test:skip:locked:001"
            ))
            await session2.execute(delete(Contact).where(Contact.phone == "+254****9999"))
            await session2.execute(delete(Business).where(Business.name == "Test Shop 2"))
            await session2.commit()


@pytest.mark.asyncio
class TestWebhookDedup:
    """Layer 11: Webhook deduplication — UNIQUE(provider_event_id)."""

    async def test_duplicate_delivery_callback_ignored(self):
        """Twilio/Africa's Talking may send the same callback twice. DB should ignore the second."""
        factory = get_session_factory()
        async with factory() as session:
            # Create a real OutboundMessage first (FK requirement)
            business = Business(name="Test Shop 3", timezone="Africa/Nairobi")
            session.add(business)
            await session.flush()

            contact = Contact(
                business_id=business.id,
                first_name="Test",
                phone="+254****8888",
                sms_opt_in=True,
            )
            session.add(contact)
            await session.flush()

            msg = OutboundMessage(
                business_id=business.id,
                contact_id=contact.id,
                message_key="test:webhook:dedup:001",
                reminder_type=ReminderType.DUE_14,
                status=MessageStatus.SENT,
                body="Test",
                segments=1,
                provider="africas_talking",
                client_message_id="test:webhook:dedup:001",
            )
            session.add(msg)
            await session.commit()

            # First callback
            cb1 = DeliveryCallback(
                message_id=msg.id,
                provider="africas_talking",
                provider_event_id="AT123:delivered",
                provider_status="delivered",
            )
            session.add(cb1)
            await session.commit()

            # Second callback (same event_id) — should fail on UNIQUE constraint
            cb2 = DeliveryCallback(
                message_id=msg.id,
                provider="africas_talking",
                provider_event_id="AT123:delivered",  # same!
                provider_status="delivered",
            )
            session.add(cb2)
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            # Cleanup
            from sqlalchemy import delete
            await session.execute(delete(DeliveryCallback).where(
                DeliveryCallback.provider_event_id == "AT123:delivered"
            ))
            await session.execute(delete(OutboundMessage).where(
                OutboundMessage.message_key == "test:webhook:dedup:001"
            ))
            await session.execute(delete(Contact).where(Contact.phone == "+254****8888"))
            await session.execute(delete(Business).where(Business.name == "Test Shop 3"))
            await session.commit()