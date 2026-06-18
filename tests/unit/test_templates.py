"""
tests/unit/test_templates.py — Unit tests for bilingual template rendering
═══════════════════════════════════════════════════

Tests:
  - English template rendering
  - Swahili template rendering
  - KES currency formatting
  - SMS segment counting (GSM-7 vs UCS-2)
  - Missing placeholder handling
  - Segment overflow detection

═══════════════════════════════════════════════════
"""

import pytest

from domain.templates import TemplateRenderer, TEMPLATES


class TestEnglishTemplates:
    """Test English template rendering."""

    def setup_method(self):
        self.renderer = TemplateRenderer()

    def test_reminder_due_14_english(self):
        body = self.renderer.render("reminder_due_14", {
            "guardian_name": "John",
            "school_name": "Mama Mboga Shop",
            "student_name": "Mary",
            "amount_due": "1500",
            "due_date": "2024-01-29",
        }, force_language="en")
        assert "John" in body
        assert "Mama Mboga Shop" in body
        assert "1500" in body
        assert "KES" in body or "KSh" in body

    def test_payment_confirmed_english(self):
        body = self.renderer.render("payment_confirmed", {
            "guardian_name": "Jane",
            "school_name": "Test Shop",
            "amount_paid": "500",
            "student_name": "Mary",
            "balance": "1000",
        }, force_language="en")
        assert "Jane" in body
        assert "500" in body


class TestSwahiliTemplates:
    """Test Swahili template rendering."""

    def setup_method(self):
        self.renderer = TemplateRenderer()

    def test_reminder_due_14_swahili(self):
        body = self.renderer.render("reminder_due_14", {
            "guardian_name": "John",
            "school_name": "Duka La Mama",
            "student_name": "Mary",
            "amount_due": "1500",
            "due_date": "2024-01-29",
        }, force_language="sw")
        assert "John" in body
        # Swahili should contain "ukumbusho" (reminder) or "tarehe" (date)
        # or "kipindi" — depends on translation used
        assert "Duka La Mama" in body


class TestSegmentCounting:
    """Test SMS segment counting for cost control."""

    def setup_method(self):
        self.renderer = TemplateRenderer()

    def test_short_message_is_one_segment(self):
        body = "Hello, this is a short message."
        segments = self.renderer._count_segments(body)
        assert segments == 1

    def test_long_message_is_two_segments(self):
        body = "A" * 200  # 200 chars > 160 → 2 segments
        segments = self.renderer._count_segments(body)
        assert segments == 2

    def test_unicode_message_uses_70_chars(self):
        body = "🎉" * 71  # Unicode chars, each > 70 → 2 segments
        segments = self.renderer._count_segments(body)
        assert segments >= 2


class TestMissingPlaceholders:
    """Test graceful handling of missing template variables."""

    def setup_method(self):
        self.renderer = TemplateRenderer()

    def test_missing_placeholder_filled_with_empty(self):
        body = self.renderer.render("reminder_due_14", {
            "guardian_name": "John",
            "school_name": "Shop",
            # missing: student_name, amount_due, due_date
        })
        # Should not raise, just have empty strings where missing
        assert "John" in body
        assert "Shop" in body


class TestTemplateExistence:
    """Verify all expected templates exist."""

    def test_all_reminder_templates_exist(self):
        expected = [
            "reminder_due_14",
            "reminder_due_3",
            "reminder_due_today",
            "reminder_late",
            "payment_confirmed",
            "callback_ack",
            "credit_terms_ack",
            "status_reply",
            "help_reply",
            "opt_out_confirm",
            "opt_in_confirm",
        ]
        for name in expected:
            assert name in TEMPLATES, f"Template '{name}' not found"

    def test_list_templates(self):
        renderer = TemplateRenderer()
        templates = renderer.list_templates()
        assert len(templates) >= 11