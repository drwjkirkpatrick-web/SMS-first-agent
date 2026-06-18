"""
alembic/versions/001_initial_schema.py — Full database schema

Creates all tables for the SMS-First Agent:
  - businesses (was: schools)
  - customers (was: students)
  - contacts (was: guardians)
  - customer_contact_links
  - transactions (was: invoices)
  - payments
  - outbound_messages (with UNIQUE message_key for dedup)
  - delivery_callbacks (with UNIQUE provider_event_id)
  - inbound_messages
  - credit_terms_requests (was: hardship_requests)
  - campaigns
  - customer_segments
  - segment_members
  - mpesa_payments
  - audit_events

Key constraints:
  - UNIQUE(message_key) on outbound_messages — Layer 2 of anti-duplicate
  - UNIQUE(provider_event_id) on delivery_callbacks — Layer 11
  - UNIQUE(school_id, phone) on contacts — prevents duplicate contacts
  - UNIQUE(school_id, invoice_number) on transactions
  - Partial indexes on pending messages and unknown_delivery
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Businesses ──
    op.create_table(
        "businesses",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Africa/Nairobi"),
        sa.Column("sms_opt_in_default", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("crm_adapter_type", sa.String(64), nullable=True),
        sa.Column("crm_config", sa.Text, nullable=True),
        sa.Column("reminder_policy", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── Customers ──
    op.create_table(
        "customers",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("school_id", sa.BigInteger, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("crm_student_id", sa.String(64), nullable=True, index=True),
        sa.Column("preferred_language", sa.String(2), nullable=False, server_default="en"),
        sa.Column("loyalty_points", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── Contacts ──
    op.create_table(
        "contacts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("school_id", sa.BigInteger, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("phone_hash", sa.String(64), nullable=True, index=True),
        sa.Column("sms_opt_in", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("opt_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_out_source", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("school_id", "phone", name="uq_contact_school_phone"),
    )

    # ── Customer-Contact Links ──
    op.create_table(
        "customer_contact_links",
        sa.Column("customer_id", sa.BigInteger, sa.ForeignKey("customers.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("contact_id", sa.BigInteger, sa.ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("relationship_type", sa.String(50), nullable=True),
        sa.Column("is_primary_contact", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )

    # ── Transactions ──
    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("school_id", sa.BigInteger, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("student_id", sa.BigInteger, sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("guardian_id", sa.BigInteger, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("invoice_number", sa.String(64), nullable=False),
        sa.Column("amount_due", sa.Numeric(10, 2), nullable=False),
        sa.Column("amount_paid", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("due_date", sa.Date, nullable=False, index=True),
        sa.Column("status", sa.Enum("pending", "partial", "paid", "overdue", "cancelled", name="invoice_status"), nullable=False, server_default="pending"),
        sa.Column("transaction_type", sa.Enum("sale", "credit", "layaway", "service", name="transaction_type"), nullable=False, server_default="sale"),
        sa.Column("sis_invoice_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("school_id", "invoice_number", name="uq_transaction_school_number"),
    )

    # ── Payments ──
    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("invoice_id", sa.BigInteger, sa.ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.Enum("pending", "confirmed", "reversed", name="payment_status"), nullable=False, server_default="pending"),
        sa.Column("payment_method", sa.String(50), nullable=True),
        sa.Column("external_reference", sa.String(255), nullable=True),
        sa.Column("confirmed_by", sa.String(100), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Outbound Messages ──
    op.create_table(
        "outbound_messages",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("school_id", sa.BigInteger, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("invoice_id", sa.BigInteger, sa.ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("guardian_id", sa.BigInteger, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("message_key", sa.String(255), nullable=False, unique=True),
        sa.Column("reminder_type", sa.Enum("due_14", "due_3", "due_today", "late_notice", "payment_confirmed", "callback_ack", "hardship_ack", name="reminder_type"), nullable=False),
        sa.Column("status", sa.Enum("pending", "sending", "sent", "delivered", "failed", "unknown_delivery", "suppressed", name="message_status"), nullable=False, server_default="pending"),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("segments", sa.Integer, nullable=False, server_default="1"),
        sa.Column("provider", sa.String(50), nullable=False, server_default="africas_talking"),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("client_message_id", sa.String(255), nullable=True, index=True),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="3"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppression_reason", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_outbound_pending_scheduled", "outbound_messages", ["status", "scheduled_at"], postgresql_where=sa.text("status = 'pending'"))
    op.create_index("ix_outbound_unknown", "outbound_messages", ["status", "updated_at"], postgresql_where=sa.text("status = 'unknown_delivery'"))

    # ── Delivery Callbacks ──
    op.create_table(
        "delivery_callbacks",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("message_id", sa.BigInteger, sa.ForeignKey("outbound_messages.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("provider", sa.String(50), nullable=False, server_default="africas_talking"),
        sa.Column("provider_event_id", sa.String(255), nullable=False, unique=True),
        sa.Column("provider_status", sa.String(50), nullable=False),
        sa.Column("raw_payload", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Inbound Messages ──
    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("school_id", sa.BigInteger, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("guardian_id", sa.BigInteger, sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("provider", sa.String(50), nullable=False, server_default="africas_talking"),
        sa.Column("provider_message_id", sa.String(255), nullable=False, unique=True),
        sa.Column("from_phone", sa.String(20), nullable=False),
        sa.Column("to_phone", sa.String(20), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("intent", sa.Enum("status", "paid", "call", "extension", "help", "stop", "start", "promo", "points", "book", "hours", "location", "unknown", name="inbound_intent"), nullable=False, server_default="unknown"),
        sa.Column("intent_confidence", sa.Numeric(3, 2), nullable=False, server_default="1.00"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_task_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Credit Terms Requests (was: hardship_requests) ──
    op.create_table(
        "credit_terms_requests",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("school_id", sa.BigInteger, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("guardian_id", sa.BigInteger, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("invoice_id", sa.BigInteger, sa.ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("inbound_message_id", sa.BigInteger, sa.ForeignKey("inbound_messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.Enum("requested", "under_review", "approved", "denied", "resolved", name="hardship_status"), nullable=False, server_default="requested"),
        sa.Column("request_body", sa.Text, nullable=True),
        sa.Column("staff_notes", sa.Text, nullable=True),
        sa.Column("assigned_to", sa.String(100), nullable=True),
        sa.Column("sla_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Campaigns ──
    op.create_table(
        "campaigns",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("school_id", sa.BigInteger, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("template_name", sa.String(100), nullable=False),
        sa.Column("segment_id", sa.BigInteger, sa.ForeignKey("customer_segments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.Enum("draft", "scheduled", "active", "paused", "completed", "cancelled", name="campaign_status"), nullable=False, server_default="draft"),
        sa.Column("schedule_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_per_customer_per_week", sa.Integer, nullable=False, server_default="1"),
        sa.Column("total_sent", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_delivered", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_kes", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Customer Segments ──
    op.create_table(
        "customer_segments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("school_id", sa.BigInteger, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("tags", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Segment Members ──
    op.create_table(
        "segment_members",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("segment_id", sa.BigInteger, sa.ForeignKey("customer_segments.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("customer_id", sa.BigInteger, sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("segment_id", "customer_id", name="uq_segment_customer"),
    )

    # ── M-Pesa Payments ──
    op.create_table(
        "mpesa_payments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("mpesa_ref", sa.String(64), nullable=False, unique=True),
        sa.Column("phone", sa.String(20), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("account_ref", sa.String(100), nullable=True),
        sa.Column("customer_name", sa.String(255), nullable=True),
        sa.Column("transaction_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_type", sa.String(20), nullable=False, server_default="c2b"),
        sa.Column("status", sa.String(20), nullable=False, server_default="received"),
        sa.Column("matched_transaction_id", sa.BigInteger, sa.ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("matched_contact_id", sa.BigInteger, sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reconciliation_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ── Audit Events ──
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("school_id", sa.BigInteger, sa.ForeignKey("businesses.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("event_type", sa.String(50), nullable=False, index=True),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("details", sa.Text, nullable=True),
        sa.Column("actor_type", sa.String(50), nullable=False, server_default="system"),
        sa.Column("actor_id", sa.String(100), nullable=True),
        sa.Column("source", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_school_created", "audit_events", ["school_id", "created_at"])
    op.create_index("ix_audit_type_created", "audit_events", ["event_type", "created_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("mpesa_payments")
    op.drop_table("segment_members")
    op.drop_table("customer_segments")
    op.drop_table("campaigns")
    op.drop_table("credit_terms_requests")
    op.drop_table("inbound_messages")
    op.drop_table("delivery_callbacks")
    op.drop_table("outbound_messages")
    op.drop_table("payments")
    op.drop_table("transactions")
    op.drop_table("customer_contact_links")
    op.drop_table("contacts")
    op.drop_table("customers")
    op.drop_table("businesses")