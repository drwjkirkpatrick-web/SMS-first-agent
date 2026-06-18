"""
adapters/crm_connector.py — CRM/POS Connector Interface
═══════════════════════════════════════════════════

A "connector" is an adapter that translates between the business's
CRM/POS system (customer database, point-of-sale, accounting) and our
database schema.

This is adapted from the original SIS (Student Information System)
connector. The pattern is identical — only the entity names change:
  - Student → Customer
  - Guardian → Contact
  - Invoice → Transaction
  - Payment → Payment (same)

Design pattern: Abstract Base Class (ABC).
  - Defines WHAT every connector must do (sync_customers, sync_transactions, etc.)
  - Concrete implementations (CSV, POS API, etc.) fill in HOW.

Teaching notes:
  - ABC enforces that all connectors implement the same methods.
  - If someone writes a new connector for a POS system, mypy will
    complain if they forget `sync_transactions()`.
  - `SyncCheckpoint` tracks the last successful sync so we only pull
    changes (incremental sync), not the entire database every time.
    This is critical for businesses with thousands of customers
    on a Raspberry Pi — we don't want to re-import everything every
    sync cycle.

Kenya-specific considerations:
  - Most Kenyan small businesses use Excel or paper ledgers, not
    sophisticated POS systems. The CSV connector is the primary
    implementation.
  - POS integration (for businesses that have them) would use REST
    APIs from systems like Square, Lightspeed, or local Kenyan POS
    providers.
  - The connector interface is business-type-agnostic: a clinic,
    salon, hardware store, or mama mboga all use the same interface.
═══════════════════════════════════════════════════
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Optional


@dataclass
class SyncCheckpoint:
    """
    Tracks sync progress for incremental updates.

    last_sync_at: the timestamp of the last successful sync
    last_record_id: the highest ID processed (for cursor-based pagination)
    checksum: hash of the last batch (detect if source data rewound)
    """
    last_sync_at: Optional[datetime] = None
    last_record_id: Optional[int] = None
    checksum: Optional[str] = None


@dataclass
class CustomerRecord:
    """
    Normalized customer data from any CRM/POS.

    Adapted from StudentRecord: student → customer.
    Fields are tailored for Kenyan small businesses.
    """
    external_customer_id: str          # external ID from CRM/POS
    first_name: str
    phone: str                          # E.164 format expected (+254XXXXXXX)
    preferred_language: Optional[str] = None  # "en" or "sw" (English/Swahili)
    loyalty_points: Optional[int] = None      # loyalty program points


@dataclass
class ContactRecord:
    """
    Normalized contact data from any CRM/POS.

    Adapted from GuardianRecord: guardian → contact.
    A contact is anyone associated with a customer who can receive SMS:
    the customer themselves, a family member, a staff member, etc.
    """
    first_name: str
    phone: str                           # E.164 format expected
    external_contact_id: Optional[str] = None
    email: Optional[str] = None
    relationship: Optional[str] = None   # "self", "family", "staff", etc.
    is_primary: bool = True


@dataclass
class TransactionRecord:
    """
    Normalized transaction data from any CRM/POS.

    Adapted from InvoiceRecord: invoice → transaction.
    A transaction is any financial record: a sale, credit, layaway,
    service charge, etc.
    """
    external_transaction_id: str
    external_customer_id: str
    transaction_number: str
    amount_due: float              # total amount
    amount_paid: float              # amount already paid
    due_date: Optional[str] = None  # ISO 8601 date string (optional for sales)
    transaction_type: str = "sale"  # "sale", "credit", "layaway", "service"
    status: str = "pending"         # pending, partial, paid, overdue, cancelled


@dataclass
class PaymentRecord:
    """Normalized payment data from any CRM/POS (unchanged from original)."""
    external_payment_id: Optional[str] = None
    external_transaction_id: str
    amount: float
    payment_method: Optional[str] = None  # "mpesa", "cash", "card", "bank"
    paid_at: Optional[str] = None  # ISO 8601 datetime


class CRMConnector(ABC):
    """
    Abstract base class for all CRM/POS connectors.

    Each method returns an AsyncIterator so we can stream large datasets
    without loading everything into memory (important on Raspberry Pi
    with limited RAM).

    The connector is initialized with a business_id (not school_id) and
    a config dict containing connection parameters.
    """

    def __init__(self, business_id: int, config: dict):
        self.business_id = business_id
        self.config = config

    @abstractmethod
    async def get_checkpoint(self) -> SyncCheckpoint:
        """Read the last sync checkpoint from persistent storage."""
        ...

    @abstractmethod
    async def save_checkpoint(self, checkpoint: SyncCheckpoint) -> None:
        """Save checkpoint after a successful sync."""
        ...

    @abstractmethod
    async def sync_customers(self, checkpoint: SyncCheckpoint) -> AsyncIterator[CustomerRecord]:
        """Yield customers created or modified since last_sync_at."""
        ...

    @abstractmethod
    async def sync_contacts(self, checkpoint: SyncCheckpoint) -> AsyncIterator[ContactRecord]:
        """Yield contacts created or modified since last_sync_at."""
        ...

    @abstractmethod
    async def sync_transactions(self, checkpoint: SyncCheckpoint) -> AsyncIterator[TransactionRecord]:
        """Yield transactions created or modified since last_sync_at."""
        ...

    @abstractmethod
    async def sync_payments(self, checkpoint: SyncCheckpoint) -> AsyncIterator[PaymentRecord]:
        """Yield payments created or modified since last_sync_at."""
        ...

    @abstractmethod
    async def test_connection(self) -> bool:
        """Verify the connector can reach the CRM/POS data source."""
        ...