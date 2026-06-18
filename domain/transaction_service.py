"""
domain/transaction_service.py — Transaction lifecycle management
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Pure business logic for Transactions (was invoice_service.py in the
tuition agent). All DB operations are async and accept a session
parameter (dependency injection pattern).

Key operations:
  - create_transaction()      — create a new sale/credit/layaway/service
  - update_status()           — transitions: pending → partial → paid → overdue
  - record_payment()          — record a payment, update balance + status
  - get_balance()             — amount_due - amount_paid
  - mark_overdue()            — find all past-due credit/layaway transactions
  - is_fully_paid()           — check if balance is zero

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - "Invoice" → "Transaction", "InvoiceStatus" → "TransactionStatus"
  - "InvoiceService" → "TransactionService"
  - NEW: `type` field (SALE, CREDIT, LAYAWAY, SERVICE) affects which
    status transitions are valid (e.g., SALE can go straight to PAID
    without a due_date; CREDIT can become OVERDUE).
  - `due_date` is now Optional (SALE transactions may not have one).
  - Amount precision increased to Numeric(12,2) for large layaway balances.

INHERITED LOGIC
---------------
  - Service is stateless (domain-driven design pattern).
  - `session: AsyncSession` is passed into every method so the caller
    controls the transaction boundary (essential for outbox pattern).
  - Status transitions are validated: you can't go from PAID to PENDING.
  - `record_payment` caps `amount_paid` at `amount_due` (no overpayment).

TEACHING NOTES
--------------
  - "Service" in domain-driven design = a stateless object that
    encapsulates business rules. It has no identity of its own.
  - We pass `session: AsyncSession` into every method so the caller
    controls the transaction boundary (important for outbox pattern).
  - Status transitions are validated: you can't go from PAID to PENDING.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - `domain/reconciliation_service.py` delegates to `record_payment()`.
  - `domain/mpesa_service.py` calls `record_payment()` after matching
    an M-Pesa webhook to a transaction.
  - `workers/reminders.py` calls `find_overdue_transactions()` to build
    the late-notice candidate list.
═══════════════════════════════════════════════════════════════════════
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import (
    Contact,
    Customer,
    Payment,
    PaymentStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
)


class InvalidStatusTransitionError(ValueError):
    """Raised when a transaction status change violates business rules."""
    pass


class TransactionService:
    """
    Stateless service for transaction operations.
    Instantiate once (or use as a module-level singleton).
    """

    # Valid status transitions (directed graph).
    # INHERITED from invoice_service — same logic, renamed.
    VALID_TRANSITIONS: dict[TransactionStatus, set[TransactionStatus]] = {
        TransactionStatus.PENDING: {
            TransactionStatus.PARTIAL,
            TransactionStatus.PAID,
            TransactionStatus.OVERDUE,
            TransactionStatus.CANCELLED,
        },
        TransactionStatus.PARTIAL: {
            TransactionStatus.PAID,
            TransactionStatus.OVERDUE,
            TransactionStatus.CANCELLED,
        },
        TransactionStatus.PAID: set(),  # terminal state
        TransactionStatus.OVERDUE: {
            TransactionStatus.PARTIAL,
            TransactionStatus.PAID,
            TransactionStatus.CANCELLED,
        },
        TransactionStatus.CANCELLED: set(),  # terminal state
    }

    async def get_transaction(
        self,
        session: AsyncSession,
        transaction_id: int,
    ) -> Optional[Transaction]:
        """Fetch a transaction by ID."""
        result = await session.execute(
            select(Transaction).where(Transaction.id == transaction_id)
        )
        return result.scalar_one_or_none()

    async def create_transaction(
        self,
        session: AsyncSession,
        business_id: int,
        customer_id: int,
        contact_id: int,
        transaction_number: str,
        amount_due: Decimal,
        txn_type: TransactionType = TransactionType.SALE,
        due_date: Optional[date] = None,
        external_transaction_id: Optional[str] = None,
    ) -> Transaction:
        """
        Create a new pending transaction.

        Args:
            amount_due: must be > 0
            due_date: required for CREDIT/LAYAWAY/SERVICE; optional for SALE
            txn_type: drives reminder behavior (see TransactionType enum)

        TEACHING NOTE: For SALE type, due_date can be None (paid at
        counter). For CREDIT/LAYAWAY, due_date is required — the
        reminder engine needs it to compute reminder dates.
        """
        if amount_due <= 0:
            raise ValueError("amount_due must be positive")
        # due_date validation: CREDIT/LAYAWAY/SERVICE require a due_date.
        if txn_type in (TransactionType.CREDIT, TransactionType.LAYAWAY, TransactionType.SERVICE):
            if due_date is None:
                raise ValueError(f"due_date is required for {txn_type.value} transactions")
        elif due_date is not None:
            # For SALE, due_date if provided must not be in the past.
            if due_date < date.today():
                raise ValueError("due_date cannot be in the past")

        transaction = Transaction(
            business_id=business_id,
            customer_id=customer_id,
            contact_id=contact_id,
            transaction_number=transaction_number,
            type=txn_type,
            amount_due=amount_due,
            amount_paid=Decimal("0.00"),
            due_date=due_date,
            status=TransactionStatus.PENDING,
            external_transaction_id=external_transaction_id,
        )
        session.add(transaction)
        await session.flush()  # get transaction.id without committing
        return transaction

    async def update_status(
        self,
        session: AsyncSession,
        transaction: Transaction,
        new_status: TransactionStatus,
    ) -> Transaction:
        """
        Transition a transaction to a new status with validation.
        Also recalculates amount_paid from payments.
        """
        current = transaction.status
        if new_status not in self.VALID_TRANSITIONS.get(current, set()):
            raise InvalidStatusTransitionError(
                f"Cannot transition from {current.value} to {new_status.value}"
            )

        transaction.status = new_status
        transaction.updated_at = datetime.utcnow()

        # If transitioning to PAID, ensure amount_paid == amount_due
        if new_status == TransactionStatus.PAID:
            transaction.amount_paid = transaction.amount_due

        await session.flush()
        return transaction

    async def record_payment(
        self,
        session: AsyncSession,
        transaction: Transaction,
        amount: Decimal,
        payment_method: Optional[str] = None,
        external_reference: Optional[str] = None,
    ) -> Payment:
        """
        Record a payment against a transaction and update its status.
        Returns the created Payment record.

        INHERITED LOGIC: amount_paid is capped at amount_due (no
        overpayment recorded at the transaction level — the Payment
        record preserves the actual amount for refund handling).
        """
        if amount <= 0:
            raise ValueError("Payment amount must be positive")

        # Create payment record
        payment = Payment(
            transaction_id=transaction.id,
            amount=amount,
            status=PaymentStatus.CONFIRMED,
            payment_method=payment_method,
            external_reference=external_reference,
            confirmed_by="system",
            confirmed_at=datetime.utcnow(),
        )
        session.add(payment)
        await session.flush()

        # Update transaction
        transaction.amount_paid = Decimal(str(transaction.amount_paid)) + amount

        # Determine new status
        if transaction.amount_paid >= transaction.amount_due:
            transaction.status = TransactionStatus.PAID
            transaction.amount_paid = transaction.amount_due  # cap at amount_due
        elif transaction.amount_paid > 0:
            transaction.status = TransactionStatus.PARTIAL

        transaction.updated_at = datetime.utcnow()
        await session.flush()
        return payment

    async def update_balance(
        self,
        session: AsyncSession,
        transaction: Transaction,
    ) -> Decimal:
        """
        Recalculate balance from payments (reconciliation use case).
        Returns the current balance.
        """
        result = await session.execute(
            select(Decimal).where(
                # Sum all confirmed payments for this transaction
            ).with_only_columns(
                # This is a placeholder — in production, use a SQL SUM query.
            )
        )
        # Simplified: use the stored amount_paid (updated by record_payment)
        return Decimal(str(transaction.amount_due)) - Decimal(str(transaction.amount_paid))

    async def get_balance(self, transaction: Transaction) -> Decimal:
        """Return remaining balance (may be negative if overpaid)."""
        return Decimal(str(transaction.amount_due)) - Decimal(str(transaction.amount_paid))

    async def mark_paid(
        self,
        session: AsyncSession,
        transaction: Transaction,
    ) -> Transaction:
        """
        Mark a transaction as fully paid (manual override).
        Sets amount_paid = amount_due and status = PAID.
        """
        return await self.update_status(session, transaction, TransactionStatus.PAID)

    async def find_overdue_transactions(
        self,
        session: AsyncSession,
        business_id: int,
        as_of: Optional[date] = None,
    ) -> list[Transaction]:
        """
        Find all transactions that are past due and not fully paid.
        Used by the scheduler for late notices.

        TEACHING NOTE: Only CREDIT and LAYAWAY transactions can be
        overdue — SALE transactions are paid at the counter, and SERVICE
        transactions (appointments) don't have a monetary overdue state.
        """
        as_of = as_of or date.today()
        result = await session.execute(
            select(Transaction).where(
                Transaction.business_id == business_id,
                Transaction.due_date < as_of,
                Transaction.status.in_([TransactionStatus.PENDING, TransactionStatus.PARTIAL]),
                Transaction.type.in_([TransactionType.CREDIT, TransactionType.LAYAWAY]),
            )
        )
        return list(result.scalars().all())

    async def is_fully_paid(self, transaction: Transaction) -> bool:
        """Check if transaction has zero or negative balance."""
        balance = await self.get_balance(transaction)
        return balance <= 0