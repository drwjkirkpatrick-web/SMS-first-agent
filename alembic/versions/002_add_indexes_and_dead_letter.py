"""Add dead_letter_messages table and query optimization indexes

Revision ID: 002
Revises: 001
Create Date: 2026-08-02

Improvements (from docs/30-improvements.md):
  - E9: Index optimization for common query patterns
  - R4: Dead letter queue for poison messages

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - ForeignKey("schools.id")    → ForeignKey("businesses.id")
    (the tenant table was renamed School → Business in the v2 schema)
  - ForeignKey("guardians.id")   → ForeignKey("contacts.id")
    (the phone-owner table was renamed Guardian → Contact)
  - The composite index on the invoices table moved to the
    transactions table, and was renamed accordingly:
      ix_invoice_school_status_due  →  ix_txn_business_status_due
      ON invoices (school_id, status, due_date)
      → ON transactions (business_id, status, due_date)
  - The two partial indexes are UNCHANGED because the columns they
    reference (intent, status, processed_at on inbound/outbound
    messages) were not renamed in the v2 schema:
      - ix_outbound_pending_retry: outbound_messages WHERE status='pending'
      - ix_inbound_call_unprocessed: inbound_messages WHERE intent='call'
        AND processed_at IS NULL
  - The inbound_call partial index column business_id replaces the
    old school_id (the column was renamed in the v2 inbound_messages
    table), but the index NAME and WHERE clause are unchanged.
  - revision stays "002", down_revision stays "001" (this is the
    second migration, building on the initial schema).

TEACHING NOTES
--------------
  - A "dead letter" message is one that has failed too many times to
    retry (poison message). Instead of silently dropping it, we move
    it to a separate table for inspection. This is the R4 improvement.
  - The dead_letter_messages table references outbound_messages.id
    (SET NULL if the original is hard-deleted) and businesses.id /
    contacts.id (CASCADE if the tenant or contact is deleted). We
    keep the original message_key and body so staff can see exactly
    what failed and why.
  - Partial indexes (CREATE INDEX ... WHERE ...) only index rows
    matching the predicate. This keeps the index small and fast:
    we don't index 100k delivered messages just to find the 5 pending
    ones. This is the E9 improvement.
  - op.execute() runs raw SQL for partial indexes because Alembic's
    op.create_index() with postgresql_where= is finicky across
    versions; raw SQL is portable and explicit.
  - The downgrade drops indexes before the table to avoid FK/index
    dependency errors. Order matters in downgrade migrations.

═══════════════════════════════════════════════════════════════════════
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── R4: Dead Letter Messages table ──
    # Stores messages that exhausted their retries ("poison messages").
    # Keeping them in a separate table (not a status on outbound_messages)
    # means the send worker's pending query never scans them, and staff
    # can triage failures without touching the hot outbox path.
    op.create_table(
        "dead_letter_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        # Link back to the original outbound message. SET NULL if the
        # original is hard-deleted, so the dead-letter record survives
        # for audit (it carries the body + failure reason).
        sa.Column(
            "original_message_id",
            sa.BigInteger(),
            sa.ForeignKey("outbound_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Tenant scoping. CASCADE: if the business is deleted, its dead
        # letters go too (no orphaned PII). Was schools.id in the tuition agent.
        sa.Column(
            "business_id",
            sa.BigInteger(),
            sa.ForeignKey("businesses.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # The contact (phone owner) the message was sent to. CASCADE on
        # contact deletion. Was guardians.id in the tuition agent.
        sa.Column(
            "contact_id",
            sa.BigInteger(),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # The dedup key from the original message (for cross-reference).
        sa.Column("message_key", sa.String(255), nullable=False),
        # Which reminder type triggered this message (e.g., "due_14").
        sa.Column("reminder_type", sa.String(50), nullable=True),
        # The message body (for staff to see what was sent).
        sa.Column("body", sa.Text(), nullable=True),
        # Why it was dead-lettered (e.g., "exceeded 3 retries: provider 500").
        sa.Column("failure_reason", sa.Text(), nullable=True),
        # When the original message was created (for latency analysis).
        sa.Column("original_created_at", sa.DateTime(timezone=True), nullable=True),
        # When it was moved to the dead letter queue (server-set).
        sa.Column(
            "dead_lettered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ── E9: Index optimization for common query patterns ──

    # (1) Partial index: outbound_messages WHERE status='pending'
    #     Supports the retry-scan query: find pending messages whose
    #     retry_count < max_retries. Partial (only pending rows) keeps
    #     the index tiny vs. indexing all 100k+ historical messages.
    #     UNCHANGED from the tuition agent — the outbound_messages
    #     columns (status, retry_count, max_retries) were not renamed.
    op.execute(
        """
        CREATE INDEX ix_outbound_pending_retry
        ON outbound_messages (retry_count, max_retries)
        WHERE status = 'pending'
        """
    )

    # (2) Composite index: transactions(business_id, status, due_date)
    #     Supports dashboard + reminder queries that filter by business,
    #     status, and a due-date range in a single index scan.
    #     RENAMED from the tuition agent:
    #       ix_invoice_school_status_due  →  ix_txn_business_status_due
    #       ON invoices (school_id, ...)  →  ON transactions (business_id, ...)
    #     because the table (invoices→transactions) and the tenant column
    #     (school_id→business_id) were both renamed in the v2 schema.
    op.execute(
        """
        CREATE INDEX ix_txn_business_status_due
        ON transactions (business_id, status, due_date)
        """
    )

    # (3) Partial index: inbound_messages WHERE intent='call' AND processed_at IS NULL
    #     Supports the staff callback queue query: find inbound "CALL"
    #     messages not yet processed. Partial (only unprocessed 'call'
    #     rows) keeps the index minimal.
    #     The column business_id replaces the old school_id (renamed in
    #     the v2 inbound_messages table), but the index NAME and the
    #     WHERE clause (intent='call' AND processed_at IS NULL) are
    #     UNCHANGED.
    op.execute(
        """
        CREATE INDEX ix_inbound_call_unprocessed
        ON inbound_messages (business_id, intent, processed_at)
        WHERE intent = 'call' AND processed_at IS NULL
        """
    )


def downgrade() -> None:
    # ── Drop indexes (order: reverse of creation) ──
    # Drop indexes before the table to avoid dependency errors.
    op.execute("DROP INDEX IF EXISTS ix_inbound_call_unprocessed")
    op.execute("DROP INDEX IF EXISTS ix_txn_business_status_due")
    op.execute("DROP INDEX IF EXISTS ix_outbound_pending_retry")

    # ── Drop the dead letter table ──
    op.drop_table("dead_letter_messages")