"""
workers/reconciliation.py — Reconciliation tasks (inherited + extended)
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Contains periodic reconciliation tasks that resolve ambiguous states:
  1. reconcile_unknown_deliveries — query SMS provider for timed-out sends.
  2. poll_payment_updates — sync payments from CSV connector + check
     for unmatched M-Pesa payments.

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - SMS adapter: Twilio → Africa's Talking (primary) + Twilio (fallback).
  - poll_payment_updates: SIS connector → CRM/POS CSV connector.
  - NEW: M-Pesa payment polling — checks for unmatched MpesaPayment
    records and triggers the mpesa_reconciliation worker to process them.
  - School → Business, Invoice → Transaction, Guardian → Contact.

INHERITED LOGIC
---------------
  - reconcile_unknown_deliveries: query provider for UNKNOWN_DELIVERY
    messages older than N minutes, resolve to SENT/FAILED/PENDING.
  - poll_payment_updates: load connector, sync payments, match to
    transactions, suppress pending reminders for paid transactions.
  - All reconciliation operations are idempotent.

TEACHING NOTES
--------------
  - "Reconciliation" = making our database match reality. When the SMS
    provider's API times out, we don't know if the message was sent.
    We mark it UNKNOWN_DELIVERY and later query the provider to find out.
  - Rural Kenya has frequent network timeouts, so reconciliation runs
    every 5 minutes (more frequent than the original 10 minutes).
  - If the Pi crashes mid-send, messages are left in SENDING state.
    The reconciliation worker finds them (they're past the timeout
    window) and queries the provider to resolve.

KENYA-SPECIFIC CONSIDERATIONS
-----------------------------
  - Africa's Talking is queried (not Twilio) for delivery status.
  - M-Pesa payments may arrive via webhook but not match any customer
    (customer used wrong account_ref). This worker alerts staff.
  - The reconciliation interval is shorter (5 min vs 10 min) because
    rural network issues are more frequent in Kenya than in the US.
═══════════════════════════════════════════════════════════════════════
"""

from datetime import datetime

from sqlalchemy import select

from adapters.connector_factory import get_connector
from domain.models import (
    Business,
    Contact,
    MessageStatus,
    MpesaPayment,
    OutboundMessage,
    Transaction,
    TransactionStatus,
)
from domain.outbox import OutboxService
from domain.reconciliation_service import ReconciliationService
from domain.transaction_service import TransactionService
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from infra.settings import get_settings
from workers.celery_app import celery_app


# ── Task 1: Reconcile unknown deliveries ──────────────────────────


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def reconcile_unknown_deliveries(self) -> dict:
    """
    Celery task: resolve UNKNOWN_DELIVERY messages by querying the provider.

    Finds messages that have been in UNKNOWN_DELIVERY state for > N minutes
    (default 10, configured via settings.unknown_delivery_reconcile_minutes)
    and queries the SMS provider (Africa's Talking) for their actual status.

    Returns:
        {"resolved": N, "not_found": N, "failed": N, "errors": N}
    """
    import asyncio

    return asyncio.run(_async_reconcile_unknown())


async def _async_reconcile_unknown() -> dict:
    outbox = OutboxService()
    recon = ReconciliationService()
    settings = get_settings()

    # Select the SMS adapter for delivery queries.
    # In production: Africa's Talking. In testing: Mock.
    from workers.sends import _get_sms_adapter
    adapter = _get_sms_adapter(settings.default_sms_provider)

    result: dict = {"resolved": 0, "not_found": 0, "failed": 0, "errors": 0}

    async with async_session_factory() as session:
        # Find messages in UNKNOWN_DELIVERY state that are older than
        # the configured threshold (default 10 minutes).
        messages = await outbox.get_unknown_deliveries(
            session,
            older_than_minutes=settings.unknown_delivery_reconcile_minutes,
        )

        for message in messages:
            try:
                if not message.client_message_id:
                    continue

                # Query the SMS provider for delivery status.
                query_result = await adapter.query_delivery(
                    message.client_message_id
                )

                # Resolve: update the message status based on provider response.
                await recon.reconcile_unknown_delivery(
                    session, message, query_result.status
                )

                if query_result.status == "not_found":
                    # Provider never received it — safe to retry.
                    result["not_found"] += 1
                elif query_result.status in ("sent", "delivered"):
                    result["resolved"] += 1
                else:
                    result["failed"] += 1

            except Exception:
                result["errors"] += 1
                continue

        await session.commit()

    return result


# ── Task 2: Poll payment updates ──────────────────────────────────


@celery_app.task(bind=True, max_retries=3, default_retry_delay=300)
def poll_payment_updates(self, business_id: int = 1) -> dict:
    """
    Celery task: sync payment updates from CRM/POS connector and
    check for unmatched M-Pesa payments.

    For CSV connector: reads payments.csv and creates Payment records.
    For M-Pesa: checks for MpesaPayment records with no matched_transaction_id
    and triggers the mpesa_reconciliation worker.

    Returns:
        {"synced": N, "transactions_updated": N, "mpesa_unmatched": N,
         "errors": [...]}
    """
    import asyncio

    return asyncio.run(_async_poll_payments(business_id))


async def _async_poll_payments(business_id: int) -> dict:
    """
    Poll CRM/POS for payment updates and reconcile with transactions.
    Also check for unmatched M-Pesa payments that need manual attention.
    """
    from decimal import Decimal

    result: dict = {
        "synced": 0,
        "transactions_updated": 0,
        "mpesa_unmatched": 0,
        "errors": [],
    }

    async with async_session_factory() as session:
        # ── Part A: CRM/POS payment sync ──────────────────────────
        biz_result = await session.execute(
            select(Business).where(Business.id == business_id)
        )
        business = biz_result.scalar_one_or_none()
        if not business:
            return result

        # Load the connector (CSV is the default for Kenyan small businesses).
        connector_type = business.connector_type or "csv"
        connector_config = {}
        if not business.connector_config:
            connector_config = {"csv_directory": "/data/crm_exports"}
        else:
            import json
            connector_config = json.loads(business.connector_config)

        connector = get_connector(
            business_id=business_id,
            adapter_type=connector_type,
            config=connector_config,
        )
        if not connector:
            result["errors"].append(f"No connector for type {connector_type}")
        else:
            # Sync payments from the connector.
            checkpoint = await connector.get_checkpoint()
            transaction_service = TransactionService()

            async for payment_record in connector.sync_payments(checkpoint):
                try:
                    # Find transaction by external reference.
                    txn_result = await session.execute(
                        select(Transaction).where(
                            Transaction.business_id == business_id,
                            Transaction.external_transaction_id
                            == payment_record.external_transaction_id,
                        )
                    )
                    transaction = txn_result.scalar_one_or_none()
                    if not transaction:
                        continue

                    # Record the payment (idempotent via external_reference).
                    payment = await transaction_service.record_payment(
                        session=session,
                        transaction=transaction,
                        amount=Decimal(str(payment_record.amount)),
                        payment_method=payment_record.payment_method,
                        external_reference=payment_record.external_payment_id,
                    )
                    result["synced"] += 1

                    # If transaction now fully paid, suppress pending reminders.
                    if transaction.status == TransactionStatus.PAID:
                        result["transactions_updated"] += 1
                        pending = await session.execute(
                            select(OutboundMessage).where(
                                OutboundMessage.transaction_id == transaction.id,
                                OutboundMessage.status == MessageStatus.PENDING,
                            )
                        )
                        for msg in pending.scalars().all():
                            msg.status = MessageStatus.SUPPRESSED
                            msg.suppression_reason = "transaction_paid"
                        await session.flush()

                except Exception as exc:
                    result["errors"].append(str(exc))
                    continue

            # Save the checkpoint for incremental sync.
            await connector.save_checkpoint(checkpoint)

        # ── Part B: M-Pesa unmatched payment check ────────────────
        # Find M-Pesa payments that haven't been matched to a transaction.
        # These need manual reconciliation by the business owner.
        unmatched_result = await session.execute(
            select(MpesaPayment).where(
                MpesaPayment.business_id == business_id,
                MpesaPayment.matched_transaction_id.is_(None),
            )
        )
        unmatched_mpesa = list(unmatched_result.scalars().all())
        result["mpesa_unmatched"] = len(unmatched_mpesa)

        # For each unmatched payment, try to match it now (the customer
        # may have been added since the webhook arrived).
        from domain.mpesa_service import MpesaService
        mpesa_service = MpesaService()

        for mpesa_payment in unmatched_mpesa:
            try:
                # Attempt to match by phone + amount.
                transaction = await mpesa_service._find_matching_transaction(
                    session=session,
                    business_id=business_id,
                    phone=mpesa_payment.phone,
                    amount=Decimal(str(mpesa_payment.amount)),
                    account_ref=mpesa_payment.account_ref,
                )
                if transaction:
                    # Match found! Record the payment.
                    payment = await mpesa_service.reconciliation_service.reconcile_payment(
                        session=session,
                        transaction=transaction,
                        amount=Decimal(str(mpesa_payment.amount)),
                        payment_method="mpesa_c2b",
                        external_reference=mpesa_payment.mpesa_ref,
                        confirmed_by="mpesa_reconciliation_worker",
                    )
                    mpesa_payment.payment_id = payment.id
                    mpesa_payment.matched_transaction_id = transaction.id
                    mpesa_payment.matched_at = datetime.utcnow()
                    await session.flush()

                    # Send confirmation SMS.
                    await mpesa_service._send_confirmation_sms(
                        session=session,
                        business_id=business_id,
                        contact_id=transaction.contact_id,
                        amount=Decimal(str(mpesa_payment.amount)),
                        balance=Decimal(str(transaction.amount_due))
                        - Decimal(str(transaction.amount_paid)),
                        mpesa_ref=mpesa_payment.mpesa_ref,
                        payment_method="M-Pesa",
                    )
                    result["mpesa_unmatched"] -= 1

            except Exception:
                continue

        await session.commit()

    return result