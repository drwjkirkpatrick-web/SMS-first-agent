"""
workers/mpesa_reconciliation.py — M-Pesa payment matching worker (NEW)
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Processes individual M-Pesa payments that arrived via Safaricom webhook
(C2B or STK Push). This worker:

  1. Loads the MpesaPayment record by ID.
  2. Matches the payment to a customer by phone number.
  3. Matches to a transaction if account_ref is provided (or by phone+amount).
  4. Updates the transaction balance via ReconciliationService.
  5. Sends a payment confirmation SMS to the customer.
  6. If unmatched, alerts staff (business owner) for manual reconciliation.

WHAT'S NEW (not in the tuition agent)
-------------------------------------
The tuition agent had no M-Pesa integration — it used a SIS CSV connector
for payment sync. This worker is entirely new for the Kenyan business
context, where M-Pesa is the dominant payment rail (>70% of transactions).

M-PESA FLOWS
------------
  C2B (Customer-to-Business):
    Customer sends money via M-Pesa to the business Paybill/Till number.
    Safaricom sends a C2B confirmation webhook → API creates MpesaPayment
    record → this worker processes it.

  STK Push (Lipa na M-Pesa Online):
    Business triggers STK Push → customer enters PIN → Safaricom sends
    STK callback webhook → API creates MpesaPayment record → this worker
    processes it.

MATCHING STRATEGY
-----------------
  1. If account_ref matches a transaction_number → direct match.
  2. Find the contact by phone number → find their open transactions.
  3. If payment amount exactly matches a transaction's remaining balance
     → strongest signal.
  4. If no exact match → apply to oldest open transaction (FIFO).
  5. If no open transactions → store as unmatched, alert staff.

IDEMPOTENCY
-----------
  - MpesaPayment has a UNIQUE constraint on (business_id, mpesa_ref).
    If Safaricom retries the webhook, the DB rejects the duplicate.
  - ReconciliationService.reconcile_payment() checks for existing
    Payment with the same external_reference before creating a new one.
  - The confirmation SMS message_key includes the mpesa_ref, so a retried
    webhook won't create a duplicate confirmation SMS.

TEACHING NOTES
--------------
  - This worker is triggered by the webhook handler (api/webhooks/mpesa.py)
    via process_mpesa_payment.delay(mpesa_payment_id). The webhook handler
    creates the MpesaPayment record and then dispatches this task.
  - The worker is separate from the webhook handler so that:
    a) The webhook can respond to Safaricom quickly (within 3 seconds).
    b) The matching + SMS sending happens asynchronously (might take longer).
  - Unmatched payments are flagged for manual review. The business owner
    sees them in the dashboard and can manually link them to a customer.

KENYA-SPECIFIC CONSIDERATIONS
-----------------------------
  - M-Pesa is Kenya's dominant payment rail. This worker is CRITICAL.
  - The mpesa_ref (e.g., "SI7K2P9X4") is globally unique from Safaricom.
  - Customers may use wrong account_ref (or none) → unmatched payments.
    The business owner must manually reconcile these.
  - The confirmation SMS is bilingual (EN/SW) based on customer preference.
  - STK Push requires the customer's phone to be registered for M-Pesa.
═══════════════════════════════════════════════════════════════════════
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select

from domain.models import (
    Business,
    Contact,
    MpesaPayment,
    OutboundMessage,
    Payment,
    ReminderType,
    MessageStatus,
    Transaction,
    TransactionStatus,
)
from domain.mpesa_service import MpesaService
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from workers.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_mpesa_payment(self, mpesa_payment_id: int) -> dict:
    """
    Celery task: process an individual M-Pesa payment.

    This task is dispatched by the M-Pesa webhook handler after creating
    a MpesaPayment record. It matches the payment to a customer + transaction,
    updates balances, and sends a confirmation SMS.

    Args:
        mpesa_payment_id: the ID of the MpesaPayment record to process.

    Returns:
        {"status": "matched"|"unmatched"|"error", "transaction_id": int|null,
         "customer_id": int|null, "confirmation_sent": bool}
    """
    import asyncio

    return asyncio.run(_async_process_mpesa_payment(mpesa_payment_id))


async def _async_process_mpesa_payment(mpesa_payment_id: int) -> dict:
    """
    Async implementation: match M-Pesa payment, update transaction, send SMS.

    TEACHING NOTE: We use MpesaService for the heavy lifting (matching,
    recording payment, sending confirmation). This worker is the Celery
    entry point that bridges the webhook (sync, must respond fast) to the
    domain service (async, may take longer).
    """
    mpesa_service = MpesaService()

    result: dict = {
        "status": "error",
        "transaction_id": None,
        "customer_id": None,
        "confirmation_sent": False,
    }

    async with async_session_factory() as session:
        try:
            # ── 1. Load the MpesaPayment record ────────────────────
            mp_result = await session.execute(
                select(MpesaPayment).where(MpesaPayment.id == mpesa_payment_id)
            )
            mpesa_payment = mp_result.scalar_one_or_none()
            if not mpesa_payment:
                result["status"] = "error"
                result["reason"] = "mpesa_payment_not_found"
                return result

            # Skip if already matched (idempotent — webhook may retry).
            if mpesa_payment.matched_transaction_id is not None:
                result["status"] = "already_matched"
                result["transaction_id"] = mpesa_payment.matched_transaction_id
                return result

            # ── 2. Load the business ──────────────────────────────
            biz_result = await session.execute(
                select(Business).where(Business.id == mpesa_payment.business_id)
            )
            business = biz_result.scalar_one_or_none()
            if not business:
                result["reason"] = "business_not_found"
                return result

            # ── 3. Match the payment to a transaction ──────────────
            # MpesaService._find_matching_transaction() tries:
            #   a) account_ref as transaction_number
            #   b) phone → contact → open transactions
            #   c) amount match to remaining balance
            #   d) FIFO: oldest open transaction
            transaction = await mpesa_service._find_matching_transaction(
                session=session,
                business_id=mpesa_payment.business_id,
                phone=mpesa_payment.phone,
                amount=Decimal(str(mpesa_payment.amount)),
                account_ref=mpesa_payment.account_ref,
            )

            if transaction:
                # ── 4a. Record payment against the transaction ────
                # ReconciliationService.reconcile_payment() is idempotent:
                # it checks for existing Payment with the same
                # external_reference (mpesa_ref) before creating a new one.
                payment = await mpesa_service.reconciliation_service.reconcile_payment(
                    session=session,
                    transaction=transaction,
                    amount=Decimal(str(mpesa_payment.amount)),
                    payment_method="mpesa_c2b" if mpesa_payment.source == "c2b" else "mpesa_stk",
                    external_reference=mpesa_payment.mpesa_ref,
                    confirmed_by="mpesa_reconciliation_worker",
                )

                # Link the MpesaPayment to the Payment + Transaction.
                mpesa_payment.payment_id = payment.id
                mpesa_payment.matched_transaction_id = transaction.id
                mpesa_payment.matched_at = datetime.utcnow()
                await session.flush()

                # ── 5. Send payment confirmation SMS ────────────────
                balance = Decimal(str(transaction.amount_due)) - Decimal(str(transaction.amount_paid))
                await mpesa_service._send_confirmation_sms(
                    session=session,
                    business_id=mpesa_payment.business_id,
                    contact_id=transaction.contact_id,
                    amount=Decimal(str(mpesa_payment.amount)),
                    balance=balance,
                    mpesa_ref=mpesa_payment.mpesa_ref,
                    payment_method="M-Pesa",
                )
                result["confirmation_sent"] = True

                # ── 6. If transaction now fully paid, suppress reminders ──
                if transaction.status == TransactionStatus.PAID:
                    pending_msgs = await session.execute(
                        select(OutboundMessage).where(
                            OutboundMessage.transaction_id == transaction.id,
                            OutboundMessage.status == MessageStatus.PENDING,
                        )
                    )
                    for msg in pending_msgs.scalars().all():
                        msg.status = MessageStatus.SUPPRESSED
                        msg.suppression_reason = "mpesa_payment_confirmed"
                    await session.flush()

                # ── 7. Audit log ───────────────────────────────────
                await log_audit_event(
                    event_type="mpesa.payment_received",
                    entity_type="mpesa_payment",
                    entity_id=str(mpesa_payment.id),
                    summary=(
                        f"M-Pesa payment {mpesa_payment.mpesa_ref} matched to "
                        f"transaction {transaction.id}: KES {mpesa_payment.amount}"
                    ),
                    context=AuditContext(
                        business_id=mpesa_payment.business_id,
                        actor_type="worker",
                        actor_id="mpesa_reconciliation",
                    ),
                )

                result["status"] = "matched"
                result["transaction_id"] = transaction.id
                result["customer_id"] = transaction.customer_id

            else:
                # ── 4b. Unmatched payment — alert staff ─────────────
                # The payment couldn't be matched to any transaction.
                # This happens when:
                #   - Customer used wrong account_ref
                #   - Customer's phone isn't in our contacts
                #   - No open transactions for this customer
                # We log the event so staff can review in the dashboard.
                await log_audit_event(
                    event_type="mpesa.payment_received",
                    entity_type="mpesa_payment",
                    entity_id=str(mpesa_payment.id),
                    summary=(
                        f"UNMATCHED M-Pesa payment {mpesa_payment.mpesa_ref}: "
                        f"KES {mpesa_payment.amount} from {mpesa_payment.phone}. "
                        f"Account ref: {mpesa_payment.account_ref or 'none'}. "
                        f"Manual reconciliation needed."
                    ),
                    context=AuditContext(
                        business_id=mpesa_payment.business_id,
                        actor_type="worker",
                        actor_id="mpesa_reconciliation",
                    ),
                )

                result["status"] = "unmatched"
                result["reason"] = "no_matching_transaction"

            await session.commit()

        except Exception as exc:
            await session.rollback()
            result["status"] = "error"
            result["reason"] = str(exc)
            raise

    return result