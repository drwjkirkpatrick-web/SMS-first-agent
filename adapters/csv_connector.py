"""
adapters/csv_connector.py — CSV CRM/POS Connector
═══════════════════════════════════════════════════

The simplest CRM/POS connector: reads CSV files exported from the
business's customer database, POS system, or Excel spreadsheet.

Most Kenyan small businesses track customers in a notebook or Excel.
CSV import is critical for onboarding — a business owner exports their
customer list to CSV and we import it.

Expected CSV formats:
  customers.csv:    external_customer_id,first_name,phone,preferred_language,loyalty_points
  contacts.csv:     external_contact_id,first_name,phone,email,relationship,is_primary
  transactions.csv:  external_transaction_id,external_customer_id,transaction_number,amount_due,amount_paid,due_date,transaction_type,status
  payments.csv:      external_payment_id,external_transaction_id,amount,payment_method,paid_at

Extended from the original tuition agent's csv_connector.py:
  - students → customers (with preferred_language and loyalty_points)
  - guardians → contacts (same pattern, renamed)
  - invoices → transactions (with transaction_type field)
  - payments → payments (unchanged)

Teaching notes:
  - CSV is the "lowest common denominator" — every system exports it.
  - We use Python's built-in `csv.DictReader` (no extra dependencies).
  - Dedupe: we check if a record already exists in the DB before inserting.
  - The connector stores its checkpoint in the database
    (business.crm_config JSON).
  - AsyncIterator methods allow streaming large files without loading
    everything into memory (important on Raspberry Pi).

Kenya-specific considerations:
  - Phone numbers in CSVs may be in local format (0712345678) or
    international (+254****5678). We normalize to E.164.
  - preferred_language: "en" (English) or "sw" (Swahili). Defaults to "en"
    if not specified.
  - loyalty_points: integer, may be empty in the CSV (default 0).
  - Provide Excel template downloads for businesses to fill in.
═══════════════════════════════════════════════════
"""

import csv
import json
import os
from datetime import datetime
from typing import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.crm_connector import (
    ContactRecord,
    CustomerRecord,
    CRMConnector,
    PaymentRecord,
    SyncCheckpoint,
    TransactionRecord,
)
from infra.database import async_session_factory


class CSVConnector(CRMConnector):
    """
    CRM/POS connector that reads CSV files from a local directory.

    Config expected in `crm_config` JSON:
    {
        "csv_directory": "/data/crm_exports",
        "encoding": "utf-8",
        "delimiter": ","
    }
    """

    async def get_checkpoint(self) -> SyncCheckpoint:
        """
        Load checkpoint from the business's crm_config JSON.

        The checkpoint tracks the last successful sync so we can do
        incremental imports (only new/changed records).
        """
        async with async_session_factory() as session:
            # Import here to avoid circular dependency at module level
            from domain.models import Business

            result = await session.execute(
                select(Business).where(Business.id == self.business_id)
            )
            business = result.scalar_one_or_none()
            if business and business.crm_config:
                config = json.loads(business.crm_config)
                cp = config.get("checkpoint", {})
                return SyncCheckpoint(
                    last_sync_at=datetime.fromisoformat(cp["last_sync_at"]) if cp.get("last_sync_at") else None,
                    last_record_id=cp.get("last_record_id"),
                    checksum=cp.get("checksum"),
                )
            return SyncCheckpoint()

    async def save_checkpoint(self, checkpoint: SyncCheckpoint) -> None:
        """Save checkpoint back to the business's crm_config JSON."""
        async with async_session_factory() as session:
            from domain.models import Business

            result = await session.execute(
                select(Business).where(Business.id == self.business_id)
            )
            business = result.scalar_one_or_none()
            if business:
                config = json.loads(business.crm_config or "{}")
                config["checkpoint"] = {
                    "last_sync_at": checkpoint.last_sync_at.isoformat() if checkpoint.last_sync_at else None,
                    "last_record_id": checkpoint.last_record_id,
                    "checksum": checkpoint.checksum,
                }
                business.crm_config = json.dumps(config)
                await session.commit()

    async def test_connection(self) -> bool:
        """Check if the CSV directory exists and contains files."""
        csv_dir = self.config.get("csv_directory", "/data/crm_exports")
        return os.path.isdir(csv_dir) and any(
            f.endswith(".csv") for f in os.listdir(csv_dir)
        )

    # ── Customer Sync ────────────────────────────────────────────
    # NEW: customer import with preferred_language and loyalty_points
    # Adapted from sync_students() — student → customer

    async def sync_customers(self, checkpoint: SyncCheckpoint) -> AsyncIterator[CustomerRecord]:
        """
        Read customers.csv and yield CustomerRecord objects.

        Expected columns:
          external_customer_id, first_name, phone, preferred_language, loyalty_points

        Teaching note: We use `yield` (async generator) so the caller
        can process records one at a time without loading the entire
        file into memory. This is important on Raspberry Pi with limited
        RAM — a business might have 10,000+ customers.
        """
        csv_dir = self.config.get("csv_directory", "/data/crm_exports")
        filepath = os.path.join(csv_dir, "customers.csv")
        if not os.path.exists(filepath):
            return
            yield  # type: ignore[unreachable]

        with open(filepath, "r", encoding=self.config.get("encoding", "utf-8")) as f:
            reader = csv.DictReader(f, delimiter=self.config.get("delimiter", ","))
            for row in reader:
                # Parse loyalty_points: empty string → None, else int
                loyalty_str = row.get("loyalty_points", "").strip()
                loyalty_points = int(loyalty_str) if loyalty_str else None

                yield CustomerRecord(
                    external_customer_id=row["external_customer_id"].strip(),
                    first_name=row["first_name"].strip(),
                    phone=self._normalize_phone(row["phone"].strip()),
                    preferred_language=row.get("preferred_language", "").strip() or None,
                    loyalty_points=loyalty_points,
                )

    # ── Contact Sync ─────────────────────────────────────────────
    # Adapted from sync_guardians() — guardian → contact

    async def sync_contacts(self, checkpoint: SyncCheckpoint) -> AsyncIterator[ContactRecord]:
        """
        Read contacts.csv and yield ContactRecord objects.

        Expected columns:
          external_contact_id, first_name, phone, email, relationship, is_primary
        """
        csv_dir = self.config.get("csv_directory", "/data/crm_exports")
        filepath = os.path.join(csv_dir, "contacts.csv")
        if not os.path.exists(filepath):
            return
            yield  # type: ignore[unreachable]

        with open(filepath, "r", encoding=self.config.get("encoding", "utf-8")) as f:
            reader = csv.DictReader(f, delimiter=self.config.get("delimiter", ","))
            for row in reader:
                yield ContactRecord(
                    external_contact_id=row.get("external_contact_id", "").strip() or None,
                    first_name=row["first_name"].strip(),
                    phone=self._normalize_phone(row["phone"].strip()),
                    email=row.get("email", "").strip() or None,
                    relationship=row.get("relationship", "").strip() or None,
                    is_primary=row.get("is_primary", "true").lower() in ("true", "1", "yes"),
                )

    # ── Transaction Sync ─────────────────────────────────────────
    # Adapted from sync_invoices() — invoice → transaction
    # Extended: added transaction_type field

    async def sync_transactions(self, checkpoint: SyncCheckpoint) -> AsyncIterator[TransactionRecord]:
        """
        Read transactions.csv and yield TransactionRecord objects.

        Expected columns:
          external_transaction_id, external_customer_id, transaction_number,
          amount_due, amount_paid, due_date, transaction_type, status

        Teaching note: The `transaction_type` field distinguishes:
          - sale: immediate purchase (no due date needed)
          - credit: customer owes money (due date important)
          - layaway: installment purchase (due date for final payment)
          - service: service charge (due date for payment)
        """
        csv_dir = self.config.get("csv_directory", "/data/crm_exports")
        filepath = os.path.join(csv_dir, "transactions.csv")
        if not os.path.exists(filepath):
            return
            yield  # type: ignore[unreachable]

        with open(filepath, "r", encoding=self.config.get("encoding", "utf-8")) as f:
            reader = csv.DictReader(f, delimiter=self.config.get("delimiter", ","))
            for row in reader:
                yield TransactionRecord(
                    external_transaction_id=row["external_transaction_id"].strip(),
                    external_customer_id=row["external_customer_id"].strip(),
                    transaction_number=row["transaction_number"].strip(),
                    amount_due=float(row["amount_due"]),
                    amount_paid=float(row.get("amount_paid", "0")),
                    due_date=row.get("due_date", "").strip() or None,
                    transaction_type=row.get("transaction_type", "sale").strip(),
                    status=row.get("status", "pending").strip(),
                )

    # ── Payment Sync ─────────────────────────────────────────────
    # Unchanged from original sync_payments()

    async def sync_payments(self, checkpoint: SyncCheckpoint) -> AsyncIterator[PaymentRecord]:
        """
        Read payments.csv and yield PaymentRecord objects.

        Expected columns:
          external_payment_id, external_transaction_id, amount, payment_method, paid_at
        """
        csv_dir = self.config.get("csv_directory", "/data/crm_exports")
        filepath = os.path.join(csv_dir, "payments.csv")
        if not os.path.exists(filepath):
            return
            yield  # type: ignore[unreachable]

        with open(filepath, "r", encoding=self.config.get("encoding", "utf-8")) as f:
            reader = csv.DictReader(f, delimiter=self.config.get("delimiter", ","))
            for row in reader:
                yield PaymentRecord(
                    external_payment_id=row.get("external_payment_id", "").strip() or None,
                    external_transaction_id=row["external_transaction_id"].strip(),
                    amount=float(row["amount"]),
                    payment_method=row.get("payment_method", "").strip() or None,
                    paid_at=row.get("paid_at", "").strip() or None,
                )

    # ── Phone Normalization ──────────────────────────────────────

    def _normalize_phone(self, phone: str) -> str:
        """
        Normalize a phone number to E.164 format for Kenya.

        Handles:
          - "0712345678"   → "+254****5678"
          - "254****5678"  → "+254****5678"
          - "+254****5678" → "+254****5678" (already correct)
          - "712345678"    → "+254****5678"

        Teaching note: Kenyan phone numbers are messy in spreadsheets.
        Some people write "0712", some "+254712", some "254712".
        We normalize everything to E.164 (+254XXXXXXXXX) for consistency.
        """
        # Strip whitespace and common separators
        cleaned = phone.strip().replace(" ", "").replace("-", "")

        # Already E.164
        if cleaned.startswith("+254"):
            return cleaned
        # International without +
        if cleaned.startswith("254"):
            return f"+{cleaned}"
        # Local format (0712345678)
        if cleaned.startswith("0"):
            return f"+254{cleaned[1:]}"
        # Bare number without prefix (712345678)
        if len(cleaned) == 9:
            return f"+254{cleaned}"
        # Unknown format — return as-is (let the adapter handle the error)
        return cleaned


# ── Persistence Helpers ──────────────────────────────────────────

async def persist_customers(
    session: AsyncSession,
    business_id: int,
    records: AsyncIterator[CustomerRecord],
) -> int:
    """
    Persist customer records with deduplication by external_customer_id.
    Returns count of customers inserted or updated.

    Teaching note: We use select-then-upsert (check if exists, then
    update or insert). In production with large datasets, use
    PostgreSQL's `INSERT ... ON CONFLICT DO UPDATE` for better
    performance (single query instead of two).
    """
    count = 0
    async for record in records:
        from domain.models import Customer

        # Check if customer already exists
        result = await session.execute(
            select(Customer).where(
                Customer.business_id == business_id,
                Customer.external_customer_id == record.external_customer_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            # Update existing customer
            existing.first_name = record.first_name
            existing.phone = record.phone
            existing.preferred_language = record.preferred_language or "en"
            existing.loyalty_points = record.loyalty_points or 0
            existing.updated_at = datetime.utcnow()
        else:
            # Insert new customer
            session.add(Customer(
                business_id=business_id,
                first_name=record.first_name,
                phone=record.phone,
                external_customer_id=record.external_customer_id,
                preferred_language=record.preferred_language or "en",
                loyalty_points=record.loyalty_points or 0,
            ))
        count += 1
    await session.flush()
    return count


async def persist_contacts(
    session: AsyncSession,
    business_id: int,
    records: AsyncIterator[ContactRecord],
) -> int:
    """Persist contacts with dedupe by phone per business."""
    count = 0
    async for record in records:
        from domain.models import Contact

        result = await session.execute(
            select(Contact).where(
                Contact.business_id == business_id,
                Contact.phone == record.phone,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.first_name = record.first_name
            existing.updated_at = datetime.utcnow()
        else:
            session.add(Contact(
                business_id=business_id,
                first_name=record.first_name,
                phone=record.phone,
            ))
        count += 1
    await session.flush()
    return count


async def persist_transactions(
    session: AsyncSession,
    business_id: int,
    records: AsyncIterator[TransactionRecord],
) -> int:
    """
    Persist transaction records with deduplication by external_transaction_id.

    Adapted from persist_invoices() — invoice → transaction.
    """
    count = 0
    async for record in records:
        from domain.models import Transaction

        result = await session.execute(
            select(Transaction).where(
                Transaction.business_id == business_id,
                Transaction.external_transaction_id == record.external_transaction_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.amount_due = record.amount_due
            existing.amount_paid = record.amount_paid
            existing.due_date = record.due_date
            existing.transaction_type = record.transaction_type
            existing.status = record.status
            existing.updated_at = datetime.utcnow()
        else:
            session.add(Transaction(
                business_id=business_id,
                external_transaction_id=record.external_transaction_id,
                transaction_number=record.transaction_number,
                amount_due=record.amount_due,
                amount_paid=record.amount_paid,
                due_date=record.due_date,
                transaction_type=record.transaction_type,
                status=record.status,
            ))
        count += 1
    await session.flush()
    return count