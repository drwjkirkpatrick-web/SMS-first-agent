"""
domain/reconciliation_service.py — Payment and callback reconciliation
═══════════════════════════════════════════════════════════════════════

INHERITED from the tuition agent (domain/reconciliation_service.py).
The reconciliation logic — matching payments, processing delivery
callbacks, resolving unknown deliveries — is domain-agnostic. The only
change is the import path (TransactionService instead of InvoiceService)
and the model references (Transaction instead of Invoice).

PURPOSE
-------
Handles:
  - Payment reconciliation: matching customer-reported / M-Pesa payments
    to transactions.
  - Delivery callback processing: updating message status from provider
    webhooks (Africa's Talking or Twilio).
  - Unknown delivery reconciliation: querying provider for ambiguous sends
    (rural Kenya has frequent network timeouts — this is critical).

TEACHING NOTES
--------------
  - "Reconciliation" means "making two records agree." When a customer
    texts "PAID" or when Africa's Talking sends a delivery receipt, we
    update our database to match reality.
  - All reconciliation operations are idempotent: running them twice
    produces the same final state. This is essential because webhooks
    can retry and the reconciliation worker runs on a schedule.
  - Payment reconciliation uses `external_reference` (e.g., M-Pesa code)
    to match against payment processor records.
  - The `process_delivery_callback` method creates a DeliveryCallback
    record (unique on provider_event_id) AND updates the OutboundMessage
    status. The unique constraint prevents duplicate webhook processing.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - `domain/transaction_service.py` (was invoice_service) provides
    `record_payment()` which this service delegates to.
  - `workers/reconciliation.py` calls `get_unknown_deliveries()` (via
    outbox) and `reconcile_unknown_delivery()`.
  - `api/webhooks/africas_talking.py` calls `process_delivery_callback()`.
  - `domain/mpesa_service.py` calls `reconcile_payment()` after matching
    an M-Pesa webhook to a transaction.
═══════════════════════════════════════════════════════════════════════
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.transaction_service import TransactionService
from domain.models import (
    DeliveryCallback,
    Contact,
    MessageStatus,
    OutboundMessage,
    Payment,
    PaymentStatus,
    Transaction,
)


class ReconciliationService:
    """
    Reconciles payments and delivery statuses.

    TEACHING NOTE: This service is stateless except for the
    TransactionService instance. It's safe to create once and reuse.
    """

    def __init__(self):
        self.transaction_service = TransactionService()

    async def reconcile_payment(
        self,
        session: AsyncSession,
        transaction: Transaction,
        amount: Decimal,
        payment_method: Optional[str] = None,
        external_reference: Optional[str] = None,
        confirmed_by: str = "system",
    ) -> Payment:
        """
        Record a confirmed payment and update transaction status.

        IDEMPOTENT: if a payment with the same external_reference already
        exists for this transaction, return the existing payment without
        creating a new one. This is critical for M-Pesa webhook retries —
        Safaricom can send the same confirmation twice.
        """
        # Check for existing payment by external reference
        if external_reference:
            result = await session.execute(
                select(Payment).where(
                    Payment.transaction_id == transaction.id,
                    Payment.external_reference == external_reference,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                return existing

        # Record new payment
        payment = await self.transaction_service.record_payment(
            session=session,
            transaction=transaction,
            amount=amount,
            payment_method=payment_method,
            external_reference=external_reference,
        )
        payment.confirmed_by = confirmed_by
        payment.confirmed_at = datetime.utcnow()
        await session.flush()
        return payment

    async def process_delivery_callback(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        provider_event_id: str,
        provider_status: str,
        raw_payload: Optional[str] = None,
    ) -> DeliveryCallback:
        """
        Process a delivery callback from the SMS provider.
        Updates message.status based on provider_status.

        provider_status mapping:
          "sent" / "queued" / "accepted" → MessageStatus.SENT
          "delivered" → MessageStatus.DELIVERED
          "failed" / "undelivered" / "rejected" → MessageStatus.FAILED

        DEDUPLICATION: the unique constraint on `provider_event_id`
        prevents processing the same webhook twice. If the provider
        retries, the DB insert fails and the caller can handle it.
        """
        # Create callback record (unique constraint prevents duplicates)
        callback = DeliveryCallback(
            message_id=message.id,
            provider=message.provider,
            provider_event_id=provider_event_id,
            provider_status=provider_status,
            raw_payload=raw_payload,
        )
        session.add(callback)
        await session.flush()

        # Update message status
        status_map = {
            "sent": MessageStatus.SENT,
            "queued": MessageStatus.SENT,
            "accepted": MessageStatus.SENT,
            "delivered": MessageStatus.DELIVERED,
            "failed": MessageStatus.FAILED,
            "undelivered": MessageStatus.FAILED,
            "rejected": MessageStatus.FAILED,
        }
        new_status = status_map.get(provider_status.lower())
        if new_status:
            message.status = new_status
            if new_status == MessageStatus.SENT:
                message.sent_at = message.sent_at or datetime.utcnow()
            elif new_status == MessageStatus.DELIVERED:
                message.delivered_at = datetime.utcnow()
            elif new_status == MessageStatus.FAILED:
                message.failed_at = datetime.utcnow()
            message.updated_at = datetime.utcnow()
            await session.flush()

        return callback

    async def reconcile_unknown_delivery(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        provider_status: str,  # status queried from provider API
    ) -> None:
        """
        Resolve an UNKNOWN_DELIVERY message after querying the provider.

        Args:
            provider_status: "sent", "delivered", "failed", or "not_found"

        TEACHING NOTE: "not_found" means the provider never received the
        message (e.g., the Pi crashed after sending to the provider but
        before we committed the DB update). In this case, it's safe to
        retry because the provider has no record of it.
        """
        if provider_status == "not_found":
            # Provider never received it — safe to retry
            message.status = MessageStatus.PENDING
            message.retry_count += 1
        elif provider_status in ("sent", "queued", "accepted"):
            message.status = MessageStatus.SENT
            message.sent_at = message.sent_at or datetime.utcnow()
        elif provider_status == "delivered":
            message.status = MessageStatus.DELIVERED
            message.delivered_at = datetime.utcnow()
        elif provider_status in ("failed", "undelivered", "rejected"):
            message.status = MessageStatus.FAILED
            message.failed_at = datetime.utcnow()

        message.updated_at = datetime.utcnow()
        await session.flush()