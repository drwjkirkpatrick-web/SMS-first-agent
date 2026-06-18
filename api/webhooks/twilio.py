"""
api/webhooks/twilio.py — Twilio status callback webhook (fallback adapter)
═══════════════════════════════════════════════════

Twilio is our fallback SMS provider. When Africa's Talking is down or
a message fails, we retry via Twilio.

This webhook receives delivery status callbacks from Twilio.

Security:
  - Twilio signs webhooks with HMAC-SHA1 using our Auth Token.
  - We validate the signature in the X-Twilio-Signature header.
  - If validation fails, we return 403 Forbidden.

Teaching notes:
  - Twilio webhooks are form-encoded (application/x-www-form-urlencoded).
  - The MessageSid is unique per send — we use it for dedup.
  - We use the same DeliveryCallback table as Africa's Talking.
    The `provider` field distinguishes them.
═══════════════════════════════════════════════════
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import select

from adapters.twilio_adapter import get_twilio_adapter
from domain.models import DeliveryCallback, MessageStatus, OutboundMessage
from infra.database import async_session_factory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/twilio", tags=["webhooks"])


@router.post("/status")
async def twilio_status(
    request: Request,
    message_sid: str = Form(..., alias="MessageSid"),
    message_status: str = Form(..., alias="MessageStatus"),
    to_number: str = Form(None, alias="To"),
    from_number: str = Form(None, alias="From"),
    error_code: str = Form(None, alias="ErrorCode"),
    client_id: str = Form(None),
) -> dict:
    """
    Twilio delivery status callback.

    Twilio sends this after each state transition:
    queued → sent → delivered (or failed/undelivered)
    """
    # Validate signature
    adapter = get_twilio_adapter()
    body = await request.body()
    signature = request.headers.get("X-Twilio-Signature", "")

    # Build the URL as Twilio sees it (behind proxy, use X-Forwarded headers)
    url = str(request.url)

    if not await adapter.validate_webhook_signature(body, signature, url):
        raise HTTPException(status_code=403, detail="Invalid signature")

    # Map Twilio status to our status
    status_map = {
        "queued": MessageStatus.SENT,
        "sent": MessageStatus.SENT,
        "delivered": MessageStatus.DELIVERED,
        "failed": MessageStatus.FAILED,
        "undelivered": MessageStatus.FAILED,
        "received": MessageStatus.SENT,
    }
    new_status = status_map.get(message_status.lower())

    async with async_session_factory() as session:
        # Find by provider message ID
        msg_result = await session.execute(
            select(OutboundMessage).where(
                OutboundMessage.provider_message_id == message_sid
            )
        )
        message = msg_result.scalar_one_or_none()

        if message:
            # Dedup delivery callbacks
            event_id = f"{message_sid}:{message_status}"
            existing = await session.execute(
                select(DeliveryCallback).where(
                    DeliveryCallback.provider_event_id == event_id
                )
            )
            if existing.scalar_one_or_none():
                return {"status": "ok", "duplicate": True}

            callback = DeliveryCallback(
                message_id=message.id,
                provider="twilio",
                provider_event_id=event_id,
                provider_status=message_status,
                raw_payload=f"error_code={error_code}, to={to_number}",
            )
            session.add(callback)

            if new_status:
                message.status = new_status
                if new_status == MessageStatus.DELIVERED:
                    message.delivered_at = datetime.utcnow()
                elif new_status == MessageStatus.FAILED:
                    message.failed_at = datetime.utcnow()

            await session.commit()
            logger.info(f"Twilio status: {message_sid} → {message_status}")

    return {"status": "ok"}