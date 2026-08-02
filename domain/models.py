"""
domain/models.py — SQLAlchemy ORM models (Kenyan small business SMS platform)
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
This file defines every table in the database for the SMS-First Agent —
a Kenyan small business customer engagement platform that sends reminders,
promos, loyalty updates, and payment follow-ups via SMS, with M-Pesa
payment integration and bilingual (English / Swahili) message templates.

KEY ADAPTATIONS FROM THE ORIGINAL TUITION AGENT
------------------------------------------------
  School      → Business        (the tenant / organisation)
  Student     → Customer        (the person the business serves)
  Guardian    → Contact         (the phone number owner who receives SMS)
  Invoice     → Transaction     (sale, credit, layaway, or service)
  HardshipReq → CreditTermsReq  (extended payment terms request)
  StudentGuardianLink → CustomerContactLink

NEW MODELS (Kenya-specific)
--------------------------
  Campaign         — promotional message batch with frequency cap + schedule
  CustomerSegment  — tagged group of customers for campaign targeting
  MpesaPayment     — M-Pesa C2B / STK Push payment record (deduped by ref)

INHERITED DESIGN DECISIONS (unchanged from tuition agent)
---------------------------------------------------------
  1. SQLAlchemy 2.0 style: Mapped[type] with type hints (modern, clean).
  2. Soft deletes: `deleted_at` timestamp instead of physical DELETE
     (preserves audit trail, allows recovery; required by Kenya DPA 2019).
  3. UNIQUE constraints enforce deduplication at the database level:
     - outbound_messages.message_key → no duplicate reminders / promos
     - delivery_callbacks.provider_event_id → no duplicate webhooks
     - mpesa_payments.mpesa_ref → no duplicate M-Pesa confirmations
  4. All tables have created_at / updated_at timestamps.
  5. Enum columns prevent invalid status values.

TEACHING NOTES
--------------
  - `server_default=func.now()` means PostgreSQL sets the timestamp,
    not Python. This avoids clock skew between app servers — critical
    on a Raspberry Pi that may not have NTP synced after a power cut.
  - `onupdate=func.now()` auto-updates `updated_at` on every UPDATE.
  - `Index(..., postgresql_where=...)` creates partial indexes (smaller,
    faster) — e.g., only index pending messages, not archived ones.
  - Relationships use `lazy="selectin"` to avoid N+1 query problems.
  - All monetary amounts are KES (Kenya Shillings), stored as
    Numeric(12, 2) — 12 digits of precision accommodates large
    layaway balances (up to KES 999,999,999.99).
  - The default timezone is Africa/Nairobi (EAT, UTC+3). Kenya has
    NO daylight saving time, which simplifies scheduling logic.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - `infra/database.py` defines `Base`, which all models inherit from.
  - `domain/reminder_service.py` reads Transactions + Contacts to decide
    who needs a reminder today.
  - `domain/campaign_service.py` reads CustomerSegments to build promo
    candidate lists.
  - `domain/mpesa_service.py` matches incoming MpesaPayment records to
    Transactions and triggers confirmation SMS.
  - `alembic/versions/001_initial_schema.py` is the migration that
    creates every table defined here — keep the two in sync.
═══════════════════════════════════════════════════════════════════════
"""

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.database import Base


# ═══════════════════════════════════════════════════════════════
# Enum Definitions
# ═══════════════════════════════════════════════════════════════

class TransactionStatus(str, enum.Enum):
    """
    Lifecycle of a Transaction (sale / credit / layaway / service).

    The naming mirrors the original `InvoiceStatus` so the reminder
    engine's suppression logic stays identical.
    """
    PENDING = "pending"        # created, not yet paid (or layaway in progress)
    PARTIAL = "partial"        # some payment received, balance remains
    PAID = "paid"              # fully paid / completed
    OVERDUE = "overdue"        # past due date, unpaid (credit type only)
    CANCELLED = "cancelled"    # voided by business


class TransactionType(str, enum.Enum):
    """
    What kind of transaction this is. Determines which reminder templates
    apply and whether a due_date is meaningful.

    SALE     — immediate purchase, paid at counter (due_date usually today)
    CREDIT   — customer owes money, pays later (due_date = agreed deadline)
    LAYAWAY  — installment purchase; item released when fully paid
    SERVICE  — appointment / service booking (due_date = appointment date)
    """
    SALE = "sale"
    CREDIT = "credit"
    LAYAWAY = "layaway"
    SERVICE = "service"


class PaymentStatus(str, enum.Enum):
    """Status of a recorded payment (manual, M-Pesa, cash, etc.)."""
    PENDING = "pending"        # customer claims paid, awaiting confirmation
    CONFIRMED = "confirmed"    # business or M-Pesa webhook confirmed
    REVERSED = "reversed"      # refunded or bounced (e.g., M-Pesa reversal)


class MessageStatus(str, enum.Enum):
    """
    Outbound message state machine.

    INHERITED EXACTLY from the tuition agent — the 12-layer
    anti-duplicate algorithm relies on these transitions:

        pending → sending → sent → delivered
                            ↓
                         failed (→ pending on retry)
                            ↓
                    unknown_delivery (reconciliation)
                            ↓
                        suppressed (terminal)
    """
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN_DELIVERY = "unknown_delivery"
    SUPPRESSED = "suppressed"


class ReminderType(str, enum.Enum):
    """
    Reminder / message templates the business can send.

    The first four are inherited from the tuition agent (same cadence).
    The rest are new for business context.
    """
    DUE_14 = "due_14"               # 14 days before due/appointment
    DUE_3 = "due_3"                  # 3 days before
    DUE_TODAY = "due_today"          # on the day
    LATE_NOTICE = "late_notice"     # overdue credit follow-up
    PAYMENT_CONFIRMED = "payment_confirmed"
    CALLBACK_ACK = "callback_ack"
    CREDIT_TERMS_ACK = "credit_terms_ack"   # was HARDSHIP_ACK
    APPOINTMENT_REMINDER = "appointment_reminder"  # NEW: clinic/salon
    LAYAWAY_PICKUP = "layaway_pickup"        # NEW: item ready for pickup
    PROMO = "promo"                           # NEW: promotional campaign
    LOYALTY_POINTS = "loyalty_points"        # NEW: loyalty update


class CampaignStatus(str, enum.Enum):
    """Lifecycle of a promotional Campaign."""
    DRAFT = "draft"            # created, not yet scheduled
    SCHEDULED = "scheduled"    # queued for the campaign worker
    RUNNING = "running"        # actively sending messages
    PAUSED = "paused"          # manually paused by business owner
    COMPLETED = "completed"    # all messages sent or end time passed
    CANCELLED = "cancelled"    # voided before completion


class CreditTermsStatus(str, enum.Enum):
    """
    Credit terms extension request lifecycle (was HardshipStatus).
    A customer asks for more time to pay off a credit transaction.
    """
    REQUESTED = "requested"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    DENIED = "denied"
    RESOLVED = "resolved"


class InboundIntent(str, enum.Enum):
    """
    Parsed intent from an inbound SMS keyword.

    Inherited keywords: STATUS, PAID, CALL, EXTENSION, HELP, STOP, START.
    NEW business keywords: PROMO, POINTS, BOOK, HOURS, LOCATION, BALANCE.
    """
    STATUS = "status"
    PAID = "paid"
    CALL = "call"
    EXTENSION = "extension"        # credit terms request
    HELP = "help"
    STOP = "stop"                   # opt-out (Kenya DPA 2019)
    START = "start"                 # opt back in
    PROMO = "promo"                 # NEW: request current promotions
    POINTS = "points"               # NEW: loyalty points balance
    BALANCE = "balance"             # NEW: credit/layaway balance
    BOOK = "book"                   # NEW: book appointment
    HOURS = "hours"                 # NEW: business hours
    LOCATION = "location"           # NEW: business location/address
    UNKNOWN = "unknown"


class AuditEventType(str, enum.Enum):
    """
    Enum of auditable events. Extended from the tuition agent with
    Kenya-specific events (M-Pesa, campaign, opt-out compliance).
    """
    MESSAGE_SEND_ATTEMPT = "message.send_attempt"
    MESSAGE_DELIVERED = "message.delivered"
    MESSAGE_FAILED = "message.failed"
    REMINDER_SUPPRESSED = "reminder.suppressed"
    POLICY_CHANGED = "policy.changed"
    CONTACT_OPT_OUT = "contact.opt_out"        # was GUARDIAN_OPT_OUT
    PAYMENT_RECONCILED = "payment.reconciled"
    CREDIT_TERMS_REQUESTED = "credit_terms.requested"  # was HARDSHIP_REQUESTED
    LOGIN_FAILURE = "login.failure"
    # ── NEW Kenya-specific ──
    CAMPAIGN_STARTED = "campaign.started"
    CAMPAIGN_COMPLETED = "campaign.completed"
    MPESA_PAYMENT_RECEIVED = "mpesa.payment_received"
    MPESA_STK_PUSH_SENT = "mpesa.stk_push_sent"
    CONNECTIVITY_OFFLINE = "connectivity.offline"
    CONNECTIVITY_ONLINE = "connectivity.online"


class Language(str, enum.Enum):
    """Customer's preferred message language."""
    EN = "en"   # English (official language of Kenya)
    SW = "sw"   # Swahili (national language)


# ═══════════════════════════════════════════════════════════════
# Businesses (was Schools)
# ═══════════════════════════════════════════════════════════════

class Business(Base):
    """
    A small business — the tenant that owns the customers, transactions,
    and campaigns. Multi-tenant safety: every query MUST filter by
    business_id (inherited from the tuition agent's school_id pattern).
    """
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Default timezone for all scheduling. Kenya = Africa/Nairobi (EAT).
    timezone: Mapped[str] = mapped_column(
        String(64),
        default="Africa/Nairobi",
        nullable=False,
    )
    # Business type drives default reminder templates:
    # retail, clinic, salon, hardware, farm_coop, restaurant, etc.
    business_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # SMS opt-in default for new contacts (Kenya DPA 2019: consent required).
    sms_opt_in_default: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Alphanumeric sender ID registered with Africa's Talking
    # (e.g., "MAMA-MBOGA"). Falls back to a shared shortcode if NULL.
    sender_id: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # CRM/POS connection config (encrypted at rest, application-level).
    # Was `sis_adapter_type` + `sis_config` in the tuition agent.
    connector_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    connector_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Reminder + campaign policy (owner-configurable; JSON, see policy_service).
    reminder_policy: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    customers: Mapped[List["Customer"]] = relationship(back_populates="business")
    contacts: Mapped[List["Contact"]] = relationship(back_populates="business")
    transactions: Mapped[List["Transaction"]] = relationship(back_populates="business")
    campaigns: Mapped[List["Campaign"]] = relationship(back_populates="business")
    segments: Mapped[List["CustomerSegment"]] = relationship(back_populates="business")


# ═══════════════════════════════════════════════════════════════
# Customers (was Students)
# ═══════════════════════════════════════════════════════════════

class Customer(Base):
    """
    A customer of the business. May have zero or more contacts (phone
    numbers) — e.g., a customer's own phone plus a family member's.
    """
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SECURITY: store first name only in logs (Kenya DPA data minimization).
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # External CRM/POS reference (opaque string, not PII).
    external_customer_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )

    # ── NEW: Loyalty points (Kenyan retail loyalty programs) ──
    loyalty_points: Mapped[int] = mapped_column(default=0, nullable=False)

    # ── NEW: Preferred message language ──
    # Drives template selection (English vs Swahili). See templates.py.
    preferred_language: Mapped[Language] = mapped_column(
        Enum(Language, name="language", values_callable=lambda x: [e.value for e in x]),
        default=Language.EN,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    business: Mapped["Business"] = relationship(back_populates="customers")
    contacts: Mapped[List["Contact"]] = relationship(
        secondary="customer_contact_links", back_populates="customers"
    )
    transactions: Mapped[List["Transaction"]] = relationship(back_populates="customer")


# ═══════════════════════════════════════════════════════════════
# Contacts (was Guardians) — the phone number owners
# ═══════════════════════════════════════════════════════════════

class Contact(Base):
    """
    A phone number that receives SMS. A customer may have multiple
    contacts (self, spouse, parent). The opt-in / opt-out flag lives
    here, not on the customer — consent is per phone number.
    """
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Phone in E.164 format (e.g., +254712345678 for Kenya).
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    # HMAC hash of phone for dedup queries without exposing the number.
    phone_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # Opt-in / opt-out (Kenya DPA 2019 compliance).
    sms_opt_in: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    opt_in_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    opt_out_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    opt_out_source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    business: Mapped["Business"] = relationship(back_populates="contacts")
    customers: Mapped[List["Customer"]] = relationship(
        secondary="customer_contact_links", back_populates="contacts"
    )
    transactions: Mapped[List["Transaction"]] = relationship(back_populates="contact")

    # Unique: one phone per business (prevents duplicate contacts).
    __table_args__ = (
        UniqueConstraint("business_id", "phone", name="uq_contact_business_phone"),
    )


# ═══════════════════════════════════════════════════════════════
# Customer-Contact Link (many-to-many; was StudentGuardianLink)
# ═══════════════════════════════════════════════════════════════

class CustomerContactLink(Base):
    """
    Links customers to their contacts (phone numbers). A customer can
    have multiple contacts; a contact can serve multiple customers
    (e.g., a parent whose children are all customers at a salon).
    """
    __tablename__ = "customer_contact_links"

    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Relationship: "self", "spouse", "parent", "staff", etc.
    relationship_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_primary_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# ═══════════════════════════════════════════════════════════════
# Transactions (was Invoices)
# ═══════════════════════════════════════════════════════════════

class Transaction(Base):
    """
    A sale, credit, layaway, or service transaction. Replaces the
    Invoice model from the tuition agent.

    The `type` field determines reminder behavior:
      SALE     — usually no reminders (paid at counter)
      CREDIT   — reminder schedule (14/3/today/late) applies
      LAYAWAY  — balance reminders + pickup reminder when paid
      SERVICE  — appointment reminder (date = appointment date)
    """
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Transaction reference (business-assigned or auto-generated).
    transaction_number: Mapped[str] = mapped_column(String(64), nullable=False)
    # What kind of transaction (drives reminder logic).
    type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, name="transaction_type", values_callable=lambda x: [e.value for e in x]),
        default=TransactionType.SALE,
        nullable=False,
    )

    # All amounts in KES. Numeric(12,2) handles large layaway balances.
    amount_due: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    amount_paid: Mapped[float] = mapped_column(Numeric(12, 2), default=0.00, nullable=False)
    # For SERVICE type, due_date = appointment date.
    # For SALE type, due_date may be NULL (paid immediately).
    due_date: Mapped[Optional[Date]] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, name="transaction_status", values_callable=lambda x: [e.value for e in x]),
        default=TransactionStatus.PENDING,
        nullable=False,
    )

    # External CRM/POS reference (if imported).
    external_transaction_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    business: Mapped["Business"] = relationship(back_populates="transactions")
    customer: Mapped["Customer"] = relationship(back_populates="transactions")
    contact: Mapped["Contact"] = relationship(back_populates="transactions")
    payments: Mapped[List["Payment"]] = relationship(back_populates="transaction")
    messages: Mapped[List["OutboundMessage"]] = relationship(back_populates="transaction")

    # Unique: one transaction_number per business.
    __table_args__ = (
        UniqueConstraint("business_id", "transaction_number", name="uq_txn_business_number"),
    )


# ═══════════════════════════════════════════════════════════════
# Payments (inherited)
# ═══════════════════════════════════════════════════════════════

class Payment(Base):
    """
    A payment recorded against a Transaction. May originate from:
      - Manual entry (cash at counter)
      - Customer SMS claim ("PAID") → later confirmed
      - M-Pesa C2B webhook (automatic confirmation)
      - M-Pesa STK Push callback (automatic confirmation)

    Idempotency: `external_reference` (e.g., M-Pesa code) is checked
    by the reconciliation service to prevent duplicate payment records.
    """
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status", values_callable=lambda x: [e.value for e in x]),
        default=PaymentStatus.PENDING,
        nullable=False,
    )
    # How the customer paid: "cash", "mpesa_c2b", "mpesa_stk", "manual".
    payment_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # External processor reference (e.g., M-Pesa confirmation code).
    external_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    confirmed_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    transaction: Mapped["Transaction"] = relationship(back_populates="payments")


# ═══════════════════════════════════════════════════════════════
# M-Pesa Payments (NEW)
# ═══════════════════════════════════════════════════════════════

class MpesaPayment(Base):
    """
    Raw M-Pesa payment record from Safaricom webhooks (C2B or STK Push).

    DEDUPLICATION: `mpesa_ref` (the M-Pesa confirmation code, e.g.,
    "SI7K2P9X4") is globally unique per Safaricom transaction. The
    UNIQUE constraint on (business_id, mpesa_ref) prevents processing
    the same M-Pesa confirmation twice if the webhook retries.

    After matching to a customer + transaction, a `Payment` record is
    created and linked via `payment_id`. Unmatched payments (no
    matching customer) stay here for manual reconciliation.
    """
    __tablename__ = "mpesa_payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The M-Pesa confirmation code (globally unique from Safaricom).
    mpesa_ref: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Source phone (customer's M-Pesa account phone).
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    # Amount in KES.
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    # Account reference the customer entered (e.g., customer ID or invoice #).
    account_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # "c2b" (customer sent to Paybill) or "stk" (business pushed STK).
    source: Mapped[str] = mapped_column(String(20), default="c2b", nullable=False)

    # Matched Payment record (once reconciled to a transaction).
    payment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    matched_transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    matched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Raw webhook payload (JSON string) for debugging / audit.
    raw_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Unique: one mpesa_ref per business (Safaricom code is globally unique,
    # but scoping to business adds an extra safety layer for multi-tenant).
    __table_args__ = (
        UniqueConstraint("business_id", "mpesa_ref", name="uq_mpesa_business_ref"),
    )


# ═══════════════════════════════════════════════════════════════
# Customer Segments (NEW)
# ═══════════════════════════════════════════════════════════════

class CustomerSegment(Base):
    """
    A named group of customers for campaign targeting. Members are
    defined by the `segment_members` association table, which can be
    populated manually, via CSV import, or by a query rule (e.g.,
    "all customers who haven't visited in 30 days").
    """
    __tablename__ = "customer_segments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Optional: a SQL-like rule for dynamic membership (future feature).
    rule_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    business: Mapped["Business"] = relationship(back_populates="segments")
    members: Mapped[List["SegmentMember"]] = relationship(
        back_populates="segment", cascade="all, delete-orphan"
    )
    campaigns: Mapped[List["Campaign"]] = relationship(back_populates="segment")


class SegmentMember(Base):
    """
    Association table linking customers to segments. Separate from
    CustomerSegment so we can add metadata (added_at, added_by) per
    membership without bloating the segment table.
    """
    __tablename__ = "segment_members"

    segment_id: Mapped[int] = mapped_column(
        ForeignKey("customer_segments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    segment: Mapped["CustomerSegment"] = relationship(back_populates="members")
    customer: Mapped["Customer"] = relationship()


# ═══════════════════════════════════════════════════════════════
# Campaigns (NEW)
# ═══════════════════════════════════════════════════════════════

class Campaign(Base):
    """
    A promotional message batch targeting a customer segment.

    The campaign worker (workers/campaigns.py) reads this record, builds
    OutboundMessage candidates for each segment member, applies the
    frequency cap, and inserts them into the outbox with a message_key
    that includes campaign_id + customer_id + date — so re-running
    the campaign on the same day is a no-op (deduped).
    """
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    segment_id: Mapped[int] = mapped_column(
        ForeignKey("customer_segments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Template name from domain/templates.py (e.g., "promo_message").
    template_name: Mapped[str] = mapped_column(String(100), nullable=False)

    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status", values_callable=lambda x: [e.value for e in x]),
        default=CampaignStatus.DRAFT,
        nullable=False,
    )

    # Schedule window.
    schedule_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    schedule_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Frequency cap: max promos per customer per week (Kenya SMS guidelines: 3).
    max_per_customer_per_week: Mapped[int] = mapped_column(default=3, nullable=False)

    # Tracking.
    total_candidates: Mapped[int] = mapped_column(default=0, nullable=False)
    total_sent: Mapped[int] = mapped_column(default=0, nullable=False)
    total_suppressed: Mapped[int] = mapped_column(default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    business: Mapped["Business"] = relationship(back_populates="campaigns")
    segment: Mapped["CustomerSegment"] = relationship(back_populates="campaigns")


# ═══════════════════════════════════════════════════════════════
# Outbound Messages (The Outbox + Sent Log) — inherited
# ═══════════════════════════════════════════════════════════════

class OutboundMessage(Base):
    """
    The transactional outbox. Every SMS the system intends to send is
    written here FIRST, then a worker picks it up and sends it.

    DEDUPLICATION (the most important column):
      `message_key` is a deterministic hash of all factors that uniquely
      identify a message. If the scheduler runs twice, it computes the
      same key, and the DB's UNIQUE constraint rejects the duplicate.

    The 12-layer anti-duplicate algorithm (inherited from the tuition
    agent) guarantees that no customer ever receives the same message
    twice, even across crashes, retries, and concurrent workers.
    """
    __tablename__ = "outbound_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # ── NEW: campaign link (NULL for reminders, set for promos) ──
    campaign_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ── DEDUPLICATION: the most important column ──
    # For reminders: business + customer + contact + transaction + type + due_date + policy_version
    # For campaigns: business + campaign_id + customer_id + date
    message_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    reminder_type: Mapped[ReminderType] = mapped_column(
        Enum(ReminderType, name="reminder_type", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name="message_status", values_callable=lambda x: [e.value for e in x]),
        default=MessageStatus.PENDING,
        nullable=False,
    )

    # Content
    body: Mapped[str] = mapped_column(Text, nullable=False)
    segments: Mapped[int] = mapped_column(default=1, nullable=False)
    # Language the message was rendered in (for audit / analytics).
    language: Mapped[str] = mapped_column(String(2), default="en", nullable=False)

    # Provider tracking
    provider: Mapped[str] = mapped_column(String(50), default="africas_talking", nullable=False)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    client_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

    # Retry tracking
    retry_count: Mapped[int] = mapped_column(default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(default=3, nullable=False)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Timing
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    suppression_reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # SMS cost tracking (KES) — populated by send worker after provider response
    price_kes: Mapped[Optional[float]] = mapped_column(Numeric(8, 2), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    transaction: Mapped[Optional["Transaction"]] = relationship(back_populates="messages")
    callbacks: Mapped[List["DeliveryCallback"]] = relationship(back_populates="message")

    # Indexes for common queries (partial indexes = smaller, faster)
    __table_args__ = (
        # Fast lookup of pending messages ordered by scheduled time
        Index(
            "ix_outbound_pending_scheduled",
            "status",
            "scheduled_at",
            postgresql_where="status = 'pending'",
        ),
        # Fast lookup of unknown_delivery messages for reconciliation
        Index(
            "ix_outbound_unknown",
            "status",
            "updated_at",
            postgresql_where="status = 'unknown_delivery'",
        ),
    )


# ═══════════════════════════════════════════════════════════════
# Delivery Callbacks (Webhook receipts) — inherited
# ═══════════════════════════════════════════════════════════════

class DeliveryCallback(Base):
    """
    Delivery receipt from the SMS provider (Africa's Talking or Twilio).

    DEDUPLICATION: `provider_event_id` is unique per provider event.
    If the provider retries the webhook, the DB rejects the duplicate.
    """
    __tablename__ = "delivery_callbacks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("outbound_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    provider: Mapped[str] = mapped_column(String(50), default="africas_talking", nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    provider_status: Mapped[str] = mapped_column(String(50), nullable=False)
    raw_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    message: Mapped["OutboundMessage"] = relationship(back_populates="callbacks")


# ═══════════════════════════════════════════════════════════════
# Inbound Messages (SMS from customers) — inherited + extended
# ═══════════════════════════════════════════════════════════════

class InboundMessage(Base):
    """
    An SMS received from a customer. The inbound worker parses the
    keyword into an `InboundIntent` and dispatches the appropriate
    response (status reply, payment claim, opt-out, etc.).
    """
    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Provider info
    provider: Mapped[str] = mapped_column(String(50), default="africas_talking", nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # Content
    from_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    to_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Parsed intent
    intent: Mapped[InboundIntent] = mapped_column(
        Enum(InboundIntent, name="inbound_intent", values_callable=lambda x: [e.value for e in x]),
        default=InboundIntent.UNKNOWN,
        nullable=False,
    )
    intent_confidence: Mapped[float] = mapped_column(Numeric(3, 2), default=1.00, nullable=False)

    # Processing
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ═══════════════════════════════════════════════════════════════
# Credit Terms Requests (was Hardship Requests)
# ═══════════════════════════════════════════════════════════════

class CreditTermsRequest(Base):
    """
    A customer's request for extended payment terms on a credit or
    layaway transaction. The business owner / staff must review and
    respond manually within the SLA window (default 24 hours).
    """
    __tablename__ = "credit_terms_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
    )
    inbound_message_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("inbound_messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[CreditTermsStatus] = mapped_column(
        Enum(CreditTermsStatus, name="credit_terms_status", values_callable=lambda x: [e.value for e in x]),
        default=CreditTermsStatus.REQUESTED,
        nullable=False,
    )

    # Customer's explanation (from SMS body).
    request_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Staff response notes.
    staff_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # SLA tracking (created_at + 24h by default).
    sla_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ═══════════════════════════════════════════════════════════════
# Audit Events (Immutable, Append-Only) — inherited
# ═══════════════════════════════════════════════════════════════

class AuditEvent(Base):
    """
    Immutable audit log. Required for Kenya Data Protection Act (2019)
    compliance: every access, change, opt-out, and payment event is
    recorded here permanently.

    This table is NEVER updated or deleted — only INSERT. The
    `created_at` timestamp is the event time.
    """
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("businesses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    event_type: Mapped[AuditEventType] = mapped_column(
        Enum(AuditEventType, name="audit_event_type", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    actor_type: Mapped[str] = mapped_column(String(50), default="system", nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Indexes for time-range queries (audit reports).
    __table_args__ = (
        Index("ix_audit_business_created", "business_id", "created_at"),
        Index("ix_audit_type_created", "event_type", "created_at"),
    )


# ═══════════════════════════════════════════════════════════════
# Dead Letter Messages (R4: Messages that exhausted retries)
# ═══════════════════════════════════════════════════════════════

class DeadLetterMessageModel(Base):
    """
    Persistence model for dead-lettered messages.

    Messages that have exhausted their retry budget (retry_count >=
    max_retries) or suffered a non-retryable failure are moved here
    instead of being silently discarded. This preserves the message
    content and failure context for manual investigation or replay.

    The table is separate from outbound_messages so that retention
    purges on the outbox don't lose dead-lettered messages.

    R4: Dead Letter Queue for poison messages.
    """
    __tablename__ = "dead_letter_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    original_message_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("outbound_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_key: Mapped[str] = mapped_column(String(255), nullable=False)
    reminder_type: Mapped[str] = mapped_column(String(50), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    original_created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    dead_lettered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_dead_letter_business_created", "business_id", "dead_lettered_at"),
    )