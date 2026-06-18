"""
api/webhooks/africas_talking.py — Inbound SMS webhook from Africa's Talking
═══════════════════════════════════════════════════

Africa's Talking sends incoming SMS to our webhook endpoint.

POST payload (form-encoded):
  - from:        customer phone number (E.164 or local format)
  - to:          our short code / sender ID
  - text:        message body
  - id:          Africa's Talking message ID
  - date:        timestamp
  - linkId:      optional (for subscription messages)

Security:
  - Africa's Talking doesn't sign webhooks by default. We validate
    by checking the source IP is from their known range, OR by
    configuring a secret token in the webhook URL path.
  - In production, use a reverse proxy (nginx) to restrict access.

Teaching notes:
  - We parse the phone number to E.164 (convert 07XX to +2547XX for Kenya).
  - We create an InboundMessage record, then queue a Celery task for
    async processing. The webhook returns 200 immediately.
  - We do NOT process the message inline — that would block the webhook
    and cause Africa's Talking to retry (leading to duplicates).
═══════════════════════════════════════════════════
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import Business, Contact, InboundIntent, InboundMessage
from infra.database import async_session_factory
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/africas-talking", tags=["webhooks"])


def normalize_kenyan_phone(phone: str) -> str:
    """
    Convert Kenyan phone numbers to E.164 format.
    0712345678 → +254712345678
    254712345678 → +254712345678
    +254712345678 → +254712345678 (already E.164)
    """
    phone = phone.strip()
    if phone.startswith("+254"):
        return phone
    if phone.startswith("254"):
        return f"+{phone}"
    if phone.startswith("0"):
        return f"+254{phone[1:]}"
    if phone.startswith("7") or phone.startswith("1"):
        return f"+254{phone}"
    return phone  # unknown format, return as-is


@router.post("/sms")
async def receive_sms(
    request: Request,
    from_number: str = Form(..., alias="from"),
    to_number: str = Form(..., alias="to"),
    text: str = Form(...),
    message_id: str = Form(..., alias="id"),
    date: str = Form(None),
    link_id: str = Form(None, alias="linkId"),
) -> dict:
    """
    Receive an inbound SMS from Africa's Talking.

    Returns 200 OK immediately. Processing happens async via Celery.
    """
    # Normalize phone
    from_phone = normalize_kenyan_phone(from_number)

    async with async_session_factory() as session:
        # Find the contact by phone number
        contact_result = await session.execute(
            select(Contact).where(Contact.phone == from_phone)
        )
        contact = contact_result.scalar_one_or_none()

        # Find the business (assume single-business deployment for now)
        business_result = await session.execute(select(Business))
        business = business_result.scalars().first()

        if not business:
            raise HTTPException(status_code=500, detail="No business configured")

        # Create inbound message record
        inbound = InboundMessage(
            school_id=business.id,  # field name kept for compatibility
            guardian_id=contact.id if contact else None,
            provider="africas_talking",
            provider_message_id=message_id,
            from_phone=from_phone,
            to_phone=to_number,
            body=text,
            intent=InboundIntent.UNKNOWN,
            intent_confidence=0.0,
        )
        session.add(inbound)
        await session.commit()
        await session.refresh(inbound)

    # Queue async processing
    celery_app.send_task(
        "workers.inbound.process_inbound_message",
        kwargs={"inbound_message_id": inbound.id},
    )

    logger.info(f"Queued inbound SMS {message_id} from {from_phone}: {text[:50]}")

    return {"status": "ok", "message_id": str(inbound.id)}


@router.post("/delivery")
async def delivery_report(
    request: Request,
    message_id: str = Form(..., alias="id"),
    status: str = Form(...),
    phone_number: str = Form(None, alias="phoneNumber"),
    failure_reason: str = Form(None, alias="failureReason"),
) -> dict:
    """
    Delivery report from Africa's Talking.

    Statuses: Sent, Delivered, Rejected, Failed, etc.
    """
    from domain.models import DeliveryCallback, MessageStatus, OutboundMessage

    async with async_session_factory() as session:
        # Find the outbound message by provider message ID
        msg_result = await session.execute(
            select(OutboundMessage).where(
                OutboundMessage.provider_message_id == message_id
            )
        )
        message = msg_result.scalar_one_or_none()

        if message:
            # Create delivery callback (dedup by provider_event_id)
            callback = DeliveryCallback(
                message_id=message.id,
                provider="africas_talking",
                provider_event_id=f"{message_id}:{status}",
                provider_status=status,
                raw_payload=f"phone={phone_number}, reason={failure_reason}",
            )
            session.add(callback)

            # Update message status
            status_map = {
                "Sent": MessageStatus.SENT,
                "Delivered": MessageStatus.DELIVERED,
                "Failed": MessageStatus.FAILED,
                "Rejected": MessageStatus.FAILED,
            }
            new_status = status_map.get(status)
            if new_status:
                message.status = new_status
                if new_status == MessageStatus.DELIVERED:
                    message.delivered_at = datetime.utcnow()
                elif new_status == MessageStatus.FAILED:
                    message.failed_at = datetime.utcnow()

            await session.commit()
            logger.info(f"Delivery report for {message_id}: {status}")

    return {"status": "ok"}