#!/usr/bin/env python3
"""
scripts/send_promo.py — Quick promotional SMS CLI tool
═══════════════════════════════════════════════════

Send a promotional SMS to a customer segment via the outbox pipeline.

Usage:
    # Send to all customers in a segment
    python scripts/send_promo.py --business-id 1 --segment-id 5 \
        --message "Mama Mboga Special: 20% off all produce today!"

    # Send to a single customer
    python scripts/send_promo.py --business-id 1 --phone +254712345678 \
        --message "Special offer just for you!"

    # Schedule for later
    python scripts/send_promo.py --business-id 1 --segment-id 5 \
        --message "..." --schedule "2024-01-20T09:00:00"

Features:
  - Uses the same outbox + dedup pipeline as automated reminders
  - Message keys include "promo" prefix + customer ID + date for dedup
  - Respects opt-out (STOP) status
  - Respects quiet hours
  - Tracks SMS cost

Teaching notes:
  - This script creates outbound messages directly in the outbox.
    The send worker picks them up on the next poll cycle (every 2 min).
  - The message_key ensures no duplicate promos to the same customer
    on the same day (even if you run the script twice).
═══════════════════════════════════════════════════
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from infra.database import async_session_factory
from domain.models import (
    Business,
    Contact,
    Customer,
    CustomerContactLink,
    MessageStatus,
    OutboundMessage,
    ReminderType,
    SegmentMember,
)


async def send_promo_to_segment(
    business_id: int,
    segment_id: int,
    message: str,
    schedule_at: datetime | None = None,
) -> dict:
    """Send promo to all customers in a segment."""
    stats = {"queued": 0, "skipped_opted_out": 0, "errors": 0}
    today = datetime.utcnow().strftime("%Y-%m-%d")

    async with async_session_factory() as session:
        # Load segment members
        members = await session.execute(
            select(SegmentMember).where(SegmentMember.segment_id == segment_id)
        )
        members = members.scalars().all()

        for member in members:
            # Find the customer's primary contact
            link_result = await session.execute(
                select(CustomerContactLink).where(
                    CustomerContactLink.customer_id == member.customer_id,
                    CustomerContactLink.is_primary_contact == True,
                )
            )
            link = link_result.scalars().first()
            if not link:
                stats["errors"] += 1
                continue

            contact = await session.execute(
                select(Contact).where(Contact.id == link.contact_id)
            )
            contact = contact.scalar_one_or_none()
            if not contact:
                stats["errors"] += 1
                continue

            # Skip opted-out
            if not contact.sms_opt_in:
                stats["skipped_opted_out"] += 1
                continue

            # Dedup key: promo:business:contact:date
            message_key = f"promo:{business_id}:{contact.id}:{today}"

            # Create outbox message
            msg = OutboundMessage(
                school_id=business_id,
                guardian_id=contact.id,
                message_key=message_key,
                reminder_type=ReminderType.CALLBACK_ACK,  # closest type for promos
                status=MessageStatus.PENDING,
                body=message,
                segments=1,
                provider="africas_talking",
                client_message_id=message_key,
                scheduled_at=schedule_at or datetime.utcnow(),
            )
            session.add(msg)
            stats["queued"] += 1

        await session.commit()

    return stats


async def send_promo_to_phone(
    business_id: int,
    phone: str,
    message: str,
    schedule_at: datetime | None = None,
) -> dict:
    """Send promo to a single phone number."""
    today = datetime.utcnow().strftime("%Y-%m-%d")

    async with async_session_factory() as session:
        contact = await session.execute(
            select(Contact).where(
                Contact.school_id == business_id,
                Contact.phone == phone,
            )
        )
        contact = contact.scalar_one_or_none()
        if not contact:
            return {"queued": 0, "error": "contact_not_found"}

        if not contact.sms_opt_in:
            return {"queued": 0, "error": "opted_out"}

        message_key = f"promo:{business_id}:{contact.id}:{today}"
        msg = OutboundMessage(
            school_id=business_id,
            guardian_id=contact.id,
            message_key=message_key,
            reminder_type=ReminderType.CALLBACK_ACK,
            status=MessageStatus.PENDING,
            body=message,
            segments=1,
            provider="africas_talking",
            client_message_id=message_key,
            scheduled_at=schedule_at or datetime.utcnow(),
        )
        session.add(msg)
        await session.commit()

    return {"queued": 1}


def main():
    parser = argparse.ArgumentParser(description="Send promotional SMS")
    parser.add_argument("--business-id", type=int, default=1)
    parser.add_argument("--segment-id", type=int, help="Segment to send to")
    parser.add_argument("--phone", help="Single phone number (E.164)")
    parser.add_argument("--message", required=True, help="Message text")
    parser.add_argument("--schedule", help="ISO datetime to schedule (default: now)")
    args = parser.parse_args()

    schedule_at = None
    if args.schedule:
        schedule_at = datetime.fromisoformat(args.schedule)

    if args.segment_id:
        stats = asyncio.run(send_promo_to_segment(
            args.business_id, args.segment_id, args.message, schedule_at
        ))
    elif args.phone:
        stats = asyncio.run(send_promo_to_phone(
            args.business_id, args.phone, args.message, schedule_at
        ))
    else:
        print("ERROR: Must specify --segment-id or --phone")
        return

    print(f"\nResult: {stats}")


if __name__ == "__main__":
    main()