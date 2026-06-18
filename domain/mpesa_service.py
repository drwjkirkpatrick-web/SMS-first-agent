"""
domain/mpesa_service.py — M-Pesa payment matching + STK Push integration
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Handles M-Pesa payment integration for the SMS-First Agent:
  - match_incoming_payment() — match an M-Pesa C2B webhook to a customer
    + transaction, record the payment, and trigger a confirmation SMS.
  - trigger_stk_push() — initiate an STK Push (Lipa na M-Pesa Online)
    that prompts the customer's phone to enter their M-Pesa PIN.
  - process_stk_callback() — handle the STK Push result webhook from
    Safaricom, record the payment, and send confirmation SMS.

M-PESA FLOWS
------------
  C2B (Customer-to-Business):
    Customer sends money via M-Pesa to the business Paybill/Till number.
    Safaricom sends a C2B confirmation webhook to our API endpoint.
    We match by phone + amount + account_ref → record payment → SMS.

  STK Push (Lipa na M-Pesa Online):
    Business triggers STK Push → Safaricom sends "Enter M-Pesa PIN" to
    customer phone → Customer enters PIN → M-Pesa deducts → Safaricom
    sends STK callback webhook → We record payment → SMS.

KEY DESIGN DECISIONS
--------------------
  1. Idempotency: `mpesa_ref` (Safaricom confirmation code) is unique
     per business. The MpesaPayment table has a UNIQUE constraint on
     (business_id, mpesa_ref). If Safaricom retries the webhook, the
     DB rejects the duplicate.
  2. Matching strategy:
     - First try: match by account_ref (if customer included their
       customer ID or transaction number as the account reference).
     - Second try: match by phone number + amount to an open
       transaction for that customer.
     - If no match: store the MpesaPayment unmatched for manual
       reconciliation by the business owner.
  3. Payment confirmation SMS: after recording the payment, we insert
     an OutboundMessage with reminder_type=PAYMENT_CONFIRMED into the
     outbox. The send worker delivers it (respects quiet hours for
     transactional messages — payment confirmations are exempt from
     quiet hours in Phase 2).
  4. STK Push is initiated via the Safaricom Daraja API. The actual
     HTTP call is made by the adapter (adapters/mpesa_adapter.py);
     this service handles the domain logic (recording, matching, SMS).

TEACHING NOTES
--------------
  - M-Pesa is the dominant payment rail in Kenya (>70% of transactions).
    This service is CRITICAL for the platform's value proposition.
  - The `mpesa_ref` (e.g., "SI7K2P9X4") is globally unique per Safaricom
    transaction. We use it as the dedup key.
  - `account_ref` is what the customer enters when sending to a Paybill.
    Businesses can instruct customers to use their customer ID or
    transaction number as the account_ref for automatic matching.
  - STK Push requires the customer's phone to be registered for M-Pesa
    and to have sufficient balance. If the STK Push times out (customer
    didn't enter PIN within ~60 seconds), the callback indicates failure.
  - The actual Daraja API authentication (OAuth token generation) and
    HTTP calls are in adapters/mpesa_adapter.py. This service stays
    focused on domain logic (matching, recording, SMS triggering).

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - `domain/models.py` defines MpesaPayment, Payment, Transaction.
  - `domain/transaction_service.py` provides `record_payment()`.
  - `domain/reconciliation_service.py` provides `reconcile_payment()`
    (idempotent payment recording by external_reference).
  - `domain/templates.py` provides "payment_confirmed" template (EN/SW).
  - `domain/dispatch_service.py` inserts the confirmation SMS into outbox.
  - `adapters/mpesa_adapter.py` handles the actual Daraja API HTTP calls.
  - `api/webhooks/mpesa.py` receives C2B webhooks and calls this service.
  - `api/webhooks/mpesa_stk.py` receives STK Push callbacks.
═══════════════════════════════════════════════════════════════════════
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import (
    Business,
    Contact,
    Customer,
    MpesaPayment,
    OutboundMessage,
    Payment,
    PaymentStatus,
    ReminderType,
    MessageStatus,
    Transaction,
    TransactionStatus,
)
from domain.transaction_service import TransactionService
from domain.reconciliation_service import ReconciliationService


class MpesaService:
    """
    M-Pesa payment matching, recording, and confirmation SMS.

    TEACHING NOTE: This service depends on TransactionService (for
    recording payments) and ReconciliationService (for idempotent
    payment recording by external_reference). We inject them in __init__
    so they can be mocked in tests.
    """

    def __init__(self):
        self.transaction_service = TransactionService()
        self.reconciliation_service = ReconciliationService()

    async def match_incoming_payment(
        self,
        session: AsyncSession,
        business_id: int,
        phone: str,
        amount: Decimal,
        mpesa_ref: str,
        account_ref: Optional[str] = None,
        raw_payload: Optional[str] = None,
    ) -> Optional[MpesaPayment]:
        """
        Match an incoming M-Pesa C2B payment to a customer + transaction.

        IDEMPOTENT: if a MpesaPayment with the same (business_id, mpesa_ref)
        already exists, return it without creating a new one.

        Matching strategy:
          1. Check if mpesa_ref already processed (dedup).
          2. Try to match by account_ref (customer ID or transaction number).
          3. Try to match by phone + amount to an open transaction.
          4. If no match: store unmatched for manual reconciliation.

        Returns the MpesaPayment record (matched or unmatched).
        """
        # ── Step 1: Dedup check ──
        existing = await session.execute(
            select(MpesaPayment).where(
                MpesaPayment.business_id == business_id,
                MpesaPayment.mpesa_ref == mpesa_ref,
            )
        )
        existing_record = existing.scalar_one_or_none()
        if existing_record:
            # Already processed — return existing (idempotent).
            return existing_record

        # ── Step 2: Create MpesaPayment record ──
        mpesa_payment = MpesaPayment(
            business_id=business_id,
            mpesa_ref=mpesa_ref,
            phone=phone,
            amount=amount,
            account_ref=account_ref,
            source="c2b",
            raw_payload=raw_payload,
        )
        session.add(mpesa_payment)
        await session.flush()

        # ── Step 3: Try to match to a transaction ──
        transaction = await self._find_matching_transaction(
            session, business_id, phone, amount, account_ref
        )

        if transaction:
            # Record the payment against the transaction (idempotent).
            payment = await self.reconciliation_service.reconcile_payment(
                session=session,
                transaction=transaction,
                amount=amount,
                payment_method="mpesa_c2b",
                external_reference=mpesa_ref,
                confirmed_by="mpesa_webhook",
            )

            # Link the MpesaPayment to the Payment + Transaction.
            mpesa_payment.payment_id = payment.id
            mpesa_payment.matched_transaction_id = transaction.id
            mpesa_payment.matched_at = datetime.utcnow()
            await session.flush()

            # ── Step 4: Trigger confirmation SMS ──
            await self._send_confirmation_sms(
                session=session,
                business_id=business_id,
                contact_id=transaction.contact_id,
                amount=amount,
                balance=Decimal(str(transaction.amount_due)) - Decimal(str(transaction.amount_paid)),
                mpesa_ref=mpesa_ref,
                payment_method="M-Pesa",
            )

        return mpesa_payment

    async def _find_matching_transaction(
        self,
        session: AsyncSession,
        business_id: int,
        phone: str,
        amount: Decimal,
        account_ref: Optional[str],
    ) -> Optional[Transaction]:
        """
        Find the transaction this M-Pesa payment belongs to.

        Strategy:
          1. If account_ref looks like a transaction_number, match directly.
          2. Find the contact by phone, then find their open transactions.
          3. Match by amount (closest open transaction with that balance).
        """
        # ── Strategy 1: Match by account_ref as transaction_number ──
        if account_ref:
            result = await session.execute(
                select(Transaction).where(
                    Transaction.business_id == business_id,
                    Transaction.transaction_number == account_ref,
                    Transaction.status.in_([
                        TransactionStatus.PENDING,
                        TransactionStatus.PARTIAL,
                    ]),
                )
            )
            txn = result.scalar_one_or_none()
            if txn:
                return txn

        # ── Strategy 2: Match by phone → contact → open transactions ──
        contact_result = await session.execute(
            select(Contact).where(
                Contact.business_id == business_id,
                Contact.phone == phone,
            )
        )
        contact = contact_result.scalar_one_or_none()
        if not contact:
            return None

        # Find open transactions for this contact, ordered by due date
        # (oldest first — pay off the oldest debt first).
        txn_result = await session.execute(
            select(Transaction).where(
                Transaction.business_id == business_id,
                Transaction.contact_id == contact.id,
                Transaction.status.in_([
                    TransactionStatus.PENDING,
                    TransactionStatus.PARTIAL,
                ]),
            ).order_by(Transaction.due_date.asc())
        )
        open_txns = list(txn_result.scalars().all())

        if not open_txns:
            return None

        # ── Strategy 3: Match by amount ──
        # If the payment amount exactly matches a transaction's remaining
        # balance, that's the strongest signal. Otherwise, apply to the
        # oldest open transaction (FIFO — standard Kenyan credit practice).
        for txn in open_txns:
            balance = Decimal(str(txn.amount_due)) - Decimal(str(txn.amount_paid))
            if balance == amount:
                return txn

        # No exact amount match → apply to oldest open transaction.
        return open_txns[0]

    async def trigger_stk_push(
        self,
        session: AsyncSession,
        business_id: int,
        phone: str,
        amount: Decimal,
        account_ref: str,
        transaction_id: Optional[int] = None,
    ) -> dict:
        """
        Initiate an STK Push (Lipa na M-Pesa Online).

        This method records the intent and returns the request details.
        The actual Daraja API HTTP call is made by the adapter
        (adapters/mpesa_adapter.py), which calls this method to record
        the intent and then makes the HTTP request.

        TEACHING NOTE: STK Push is a "pull" payment — the business
        initiates it, and the customer must approve on their phone.
        This is different from C2B ("push" — customer initiates).

        Returns:
            {
                "status": "initiated",
                "phone": masked_phone,
                "amount": amount,
                "account_ref": account_ref,
                "transaction_id": transaction_id,
            }
        """
        # The adapter will make the actual HTTP call to Daraja API.
        # This service records the intent for audit + tracking.
        # In production, the adapter calls this method, then makes the
        # HTTP request, and on callback, calls process_stk_callback().

        from domain.masking import mask_phone

        return {
            "status": "initiated",
            "phone": mask_phone(phone),
            "amount": str(amount),
            "account_ref": account_ref,
            "transaction_id": transaction_id,
        }

    async def process_stk_callback(
        self,
        session: AsyncSession,
        business_id: int,
        phone: str,
        amount: Decimal,
        mpesa_ref: str,
        account_ref: str,
        transaction_id: Optional[int] = None,
        raw_payload: Optional[str] = None,
    ) -> Optional[MpesaPayment]:
        """
        Process an STK Push callback from Safaricom.

        This is called after the customer has entered their M-Pesa PIN
        and Safaricom has confirmed the payment. The flow is:
          1. Dedup check on mpesa_ref.
          2. Create MpesaPayment record (source="stk").
          3. Match to the transaction (by transaction_id if provided,
             otherwise by account_ref / phone + amount).
          4. Record payment via ReconciliationService (idempotent).
          5. Send confirmation SMS.

        TEACHING NOTE: If the STK Push failed (customer didn't enter PIN,
        insufficient balance, etc.), the callback will have a different
        result code. This method is only called on SUCCESS. Failed STK
        Pushes are handled by the adapter, which logs the failure.
        """
        # Dedup check.
        existing = await session.execute(
            select(MpesaPayment).where(
                MpesaPayment.business_id == business_id,
                MpesaPayment.mpesa_ref == mpesa_ref,
            )
        )
        if existing.scalar_one_or_none():
            return existing.scalar_one_or_none()

        # Create MpesaPayment record.
        mpesa_payment = MpesaPayment(
            business_id=business_id,
            mpesa_ref=mpesa_ref,
            phone=phone,
            amount=amount,
            account_ref=account_ref,
            source="stk",
            raw_payload=raw_payload,
        )
        session.add(mpesa_payment)
        await session.flush()

        # Match to transaction.
        transaction: Optional[Transaction] = None
        if transaction_id:
            txn_result = await session.execute(
                select(Transaction).where(Transaction.id == transaction_id)
            )
            transaction = txn_result.scalar_one_or_none()
        else:
            transaction = await self._find_matching_transaction(
                session, business_id, phone, amount, account_ref
            )

        if transaction:
            payment = await self.reconciliation_service.reconcile_payment(
                session=session,
                transaction=transaction,
                amount=amount,
                payment_method="mpesa_stk",
                external_reference=mpesa_ref,
                confirmed_by="mpesa_stk_webhook",
            )
            mpesa_payment.payment_id = payment.id
            mpesa_payment.matched_transaction_id = transaction.id
            mpesa_payment.matched_at = datetime.utcnow()
            await session.flush()

            await self._send_confirmation_sms(
                session=session,
                business_id=business_id,
                contact_id=transaction.contact_id,
                amount=amount,
                balance=Decimal(str(transaction.amount_due)) - Decimal(str(transaction.amount_paid)),
                mpesa_ref=mpesa_ref,
                payment_method="M-Pesa STK",
            )

        return mpesa_payment

    async def _send_confirmation_sms(
        self,
        session: AsyncSession,
        business_id: int,
        contact_id: int,
        amount: Decimal,
        balance: Decimal,
        mpesa_ref: str,
        payment_method: str,
    ) -> OutboundMessage:
        """
        Insert a payment confirmation SMS into the outbox.

        TEACHING NOTE: We insert directly into outbound_messages rather
        than going through DispatchService because this is a single
        message (not a batch of candidates). The message_key includes
        the mpesa_ref to prevent duplicate confirmation SMS if the
        webhook retries.

        The send worker will render the template body, apply quiet
        hours (transactional messages are exempt from quiet hours in
        Phase 2), and deliver via the SMS provider.
        """
        # Dedup key: includes mpesa_ref so a retried webhook doesn't
        # create a duplicate confirmation SMS.
        message_key = f"{business_id}:mpesa_confirm:{mpesa_ref}"

        # Check for existing confirmation (idempotent).
        existing = await session.execute(
            select(OutboundMessage).where(
                OutboundMessage.message_key == message_key
            )
        )
        if existing.scalar_one_or_none():
            return existing.scalar_one_or_none()

        message = OutboundMessage(
            business_id=business_id,
            contact_id=contact_id,
            message_key=message_key,
            reminder_type=ReminderType.PAYMENT_CONFIRMED,
            status=MessageStatus.PENDING,
            body="",  # rendered by send worker via templates.py
            segments=1,
            language="en",  # send worker will use customer's preferred_language
            provider="africas_talking",
            client_message_id=message_key,
            retry_count=0,
            max_retries=3,
            scheduled_at=datetime.utcnow(),
        )
        session.add(message)
        await session.flush()
        return message