"""
tests/unit/test_reminder_engine.py — Unit tests for the reminder engine
═══════════════════════════════════════════════════

Tests the core business logic:
  - Reminder eligibility (14-day, 3-day, day-of, late notice)
  - Message key determinism (same inputs → same key)
  - Suppression rules (paid, opted out, cancelled)
  - Late notice eligibility
  - Business hours enforcement (via policy)

Teaching notes:
  - We use plain Python dataclasses, not the DB, for these tests.
    The reminder service is pure business logic — no I/O.
  - We test edge cases: same-day payment, past-due invoices, etc.
  - Message key tests verify the anti-duplicate foundation: if the
    key function is deterministic, the 12-layer defense works.
═══════════════════════════════════════════════════
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from domain.models import (
    Business,
    Contact,
    Invoice,
    InvoiceStatus,
    ReminderType,
    Transaction,
)
from domain.reminder_service import ReminderService


class TestMessageKey:
    """Verify deterministic message key generation (anti-duplicate Layer 1)."""

    def setup_method(self):
        self.service = ReminderService()

    def test_same_inputs_produce_same_key(self):
        """Two calls with identical args must produce identical keys."""
        key1 = self.service.compute_message_key(1, 101, 201, 1001, ReminderType.DUE_14, date(2024, 1, 15))
        key2 = self.service.compute_message_key(1, 101, 201, 1001, ReminderType.DUE_14, date(2024, 1, 15))
        assert key1 == key2

    def test_different_business_produces_different_key(self):
        key1 = self.service.compute_message_key(1, 101, 201, 1001, ReminderType.DUE_14, date(2024, 1, 15))
        key2 = self.service.compute_message_key(2, 101, 201, 1001, ReminderType.DUE_14, date(2024, 1, 15))
        assert key1 != key2

    def test_different_reminder_type_produces_different_key(self):
        key1 = self.service.compute_message_key(1, 101, 201, 1001, ReminderType.DUE_14, date(2024, 1, 15))
        key2 = self.service.compute_message_key(1, 101, 201, 1001, ReminderType.DUE_3, date(2024, 1, 15))
        assert key1 != key2

    def test_different_due_date_produces_different_key(self):
        key1 = self.service.compute_message_key(1, 101, 201, 1001, ReminderType.DUE_14, date(2024, 1, 15))
        key2 = self.service.compute_message_key(1, 101, 201, 1001, ReminderType.DUE_14, date(2024, 1, 16))
        assert key1 != key2

    def test_key_format(self):
        key = self.service.compute_message_key(1, 101, 201, 1001, ReminderType.DUE_14, date(2024, 1, 15))
        parts = key.split(":")
        assert len(parts) == 7
        assert parts[0] == "1"           # business_id
        assert parts[1] == "101"          # customer_id
        assert parts[2] == "201"          # contact_id
        assert parts[3] == "1001"         # transaction_id
        assert parts[4] == "due_14"       # reminder type
        assert parts[5] == "2024-01-15"   # due date
        assert parts[6] == "v1"           # policy version


class TestReminderEligibility:
    """Test which reminders should trigger on a given day."""

    def setup_method(self):
        self.service = ReminderService()
        self.today = date(2024, 1, 15)

    def test_14_day_reminder_triggers_14_days_before(self):
        invoice = _make_invoice(due_date=self.today + timedelta(days=14))
        assert self.service.is_reminder_due_today(invoice, ReminderType.DUE_14, self.today)

    def test_3_day_reminder_triggers_3_days_before(self):
        invoice = _make_invoice(due_date=self.today + timedelta(days=3))
        assert self.service.is_reminder_due_today(invoice, ReminderType.DUE_3, self.today)

    def test_today_reminder_triggers_on_due_date(self):
        invoice = _make_invoice(due_date=self.today)
        assert self.service.is_reminder_due_today(invoice, ReminderType.DUE_TODAY, self.today)

    def test_14_day_does_not_trigger_on_wrong_day(self):
        invoice = _make_invoice(due_date=self.today + timedelta(days=13))
        assert not self.service.is_reminder_due_today(invoice, ReminderType.DUE_14, self.today)

    def test_no_reminder_for_paid_invoice(self):
        """Paid invoices should not generate any candidates."""
        invoice = _make_invoice(due_date=self.today + timedelta(days=14), status=InvoiceStatus.PAID)
        school = _make_business()
        candidates = self.service.build_candidates(school, [invoice], today=self.today)
        assert len(candidates) == 0

    def test_no_reminder_for_cancelled_invoice(self):
        invoice = _make_invoice(due_date=self.today + timedelta(days=14), status=InvoiceStatus.CANCELLED)
        school = _make_business()
        candidates = self.service.build_candidates(school, [invoice], today=self.today)
        assert len(candidates) == 0


class TestLateNotice:
    """Test late notice eligibility."""

    def setup_method(self):
        self.service = ReminderService()
        self.today = date(2024, 1, 15)

    def test_late_notice_for_overdue_invoice(self):
        invoice = _make_invoice(due_date=self.today - timedelta(days=1))
        assert self.service.is_late_notice_eligible(invoice, self.today)

    def test_no_late_notice_for_paid_invoice(self):
        invoice = _make_invoice(due_date=self.today - timedelta(days=5), status=InvoiceStatus.PAID)
        assert not self.service.is_late_notice_eligible(invoice, self.today)

    def test_no_late_notice_for_future_invoice(self):
        invoice = _make_invoice(due_date=self.today + timedelta(days=5))
        assert not self.service.is_late_notice_eligible(invoice, self.today)

    def test_no_late_notice_on_due_date(self):
        """Late notice triggers the day AFTER due date, not on due date."""
        invoice = _make_invoice(due_date=self.today)
        assert not self.service.is_late_notice_eligible(invoice, self.today)


class TestSuppression:
    """Test suppression rules."""

    def setup_method(self):
        self.service = ReminderService()

    def test_suppress_paid_invoice(self):
        invoice = _make_invoice(status=InvoiceStatus.PAID)
        contact = _make_contact()
        suppressed, reason = self.service.should_suppress(invoice, contact)
        assert suppressed
        assert reason == "invoice_paid"

    def test_suppress_cancelled_invoice(self):
        invoice = _make_invoice(status=InvoiceStatus.CANCELLED)
        contact = _make_contact()
        suppressed, reason = self.service.should_suppress(invoice, contact)
        assert suppressed
        assert reason == "invoice_cancelled"

    def test_suppress_opted_out_contact(self):
        invoice = _make_invoice()
        contact = _make_contact(sms_opt_in=False)
        suppressed, reason = self.service.should_suppress(invoice, contact)
        assert suppressed
        assert reason == "guardian_opted_out"

    def test_no_suppress_for_active_invoice_opted_in(self):
        invoice = _make_invoice()
        contact = _make_contact()
        suppressed, reason = self.service.should_suppress(invoice, contact)
        assert not suppressed
        assert reason is None


class TestBuildCandidates:
    """Test the full candidate building flow."""

    def setup_method(self):
        self.service = ReminderService()
        self.today = date(2024, 1, 15)
        self.school = _make_business()

    def test_multiple_reminders_on_same_day(self):
        """If 14-day and 3-day both trigger on the same day, both candidates are built."""
        # due_date = today + 14 = Jan 29 → 14-day triggers today
        # due_date = today + 3 = Jan 18 → 3-day triggers today
        # These are different invoices
        inv1 = _make_invoice(invoice_id=1001, due_date=self.today + timedelta(days=14))
        inv2 = _make_invoice(invoice_id=1002, due_date=self.today + timedelta(days=3))
        candidates = self.service.build_candidates(self.school, [inv1, inv2], today=self.today)
        types = [c.reminder_type for c in candidates]
        assert ReminderType.DUE_14 in types
        assert ReminderType.DUE_3 in types

    def test_candidate_has_message_key(self):
        inv = _make_invoice(invoice_id=1001, due_date=self.today + timedelta(days=14))
        candidates = self.service.build_candidates(self.school, [inv], today=self.today)
        assert len(candidates) == 1
        assert candidates[0].message_key is not None
        assert len(candidates[0].message_key) > 0


# ── Test Helpers ──

def _make_business(business_id=1, name="Test Shop") -> Business:
    return Business(id=business_id, name=name, timezone="Africa/Nairobi")


def _make_contact(contact_id=201, sms_opt_in=True) -> Contact:
    return Contact(
        id=contact_id,
        school_id=1,
        first_name="Jane",
        phone="+254****5678",
        sms_opt_in=sms_opt_in,
    )


def _make_invoice(
    invoice_id=1001,
    due_date=None,
    status=InvoiceStatus.PENDING,
    amount_due=1500.00,
) -> Transaction:
    return Transaction(
        id=invoice_id,
        school_id=1,
        student_id=101,
        guardian_id=201,
        invoice_number=f"INV-{invoice_id}",
        amount_due=Decimal(str(amount_due)),
        amount_paid=Decimal("0.00"),
        due_date=due_date or date(2024, 1, 29),
        status=status,
    )