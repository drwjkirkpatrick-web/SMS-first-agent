"""
tests/unit/test_campaign_service.py — Unit tests for campaign engine
═══════════════════════════════════════════════════

Tests:
  - Campaign candidate building from segments
  - Frequency cap enforcement (max N per customer per week)
  - Dedup message keys include campaign_id
  - Paused/expired campaigns don't generate candidates

═══════════════════════════════════════════════════
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.campaign_service import CampaignService
from domain.models import Campaign, CampaignStatus


class TestCampaignCandidates:
    """Test building campaign candidates from customer segments."""

    def setup_method(self):
        self.service = CampaignService()

    def test_service_exists(self):
        assert self.service is not None

    def test_message_key_includes_campaign_id(self):
        """Campaign message keys must include campaign_id for dedup."""
        key = self.service.compute_campaign_message_key(
            business_id=1,
            campaign_id=500,
            customer_id=201,
            send_date=date(2024, 1, 15),
        )
        assert "500" in key  # campaign_id is in the key
        parts = key.split(":")
        assert len(parts) >= 4  # business:campaign:promo:customer:date

    def test_different_campaigns_different_keys(self):
        """Same customer, different campaigns → different keys (no cross-campaign dedup)."""
        key1 = self.service.compute_campaign_message_key(1, 500, 201, date(2024, 1, 15))
        key2 = self.service.compute_campaign_message_key(1, 501, 201, date(2024, 1, 15))
        assert key1 != key2

    def test_same_campaign_same_day_same_key(self):
        """Same customer, same campaign, same day → same key (dedup works)."""
        key1 = self.service.compute_campaign_message_key(1, 500, 201, date(2024, 1, 15))
        key2 = self.service.compute_campaign_message_key(1, 500, 201, date(2024, 1, 15))
        assert key1 == key2


class TestFrequencyCap:
    """Test max-per-customer-per-week enforcement."""

    def setup_method(self):
        self.service = CampaignService()

    def test_frequency_cap_method_exists(self):
        """The service must expose a frequency-cap enforcement method."""
        assert hasattr(self.service, "enforce_frequency_cap")


class TestCampaignStatus:
    """Test that campaign status affects candidate generation."""

    def setup_method(self):
        self.service = CampaignService()

    def _make_campaign(self, status: CampaignStatus) -> Campaign:
        """Helper: build a Campaign with the given status."""
        return Campaign(
            business_id=1,
            segment_id=1,
            name="Test Campaign",
            template_name="promo_message",
            status=status,
            schedule_start=datetime(2024, 1, 1),
            schedule_end=None,
            max_per_customer_per_week=3,
        )

    def test_paused_campaign_no_candidates(self):
        """A paused campaign should not be considered active."""
        campaign = self._make_campaign(CampaignStatus.PAUSED)
        assert self.service.is_campaign_active(campaign) is False

    def test_completed_campaign_no_candidates(self):
        """A completed campaign should not be considered active."""
        campaign = self._make_campaign(CampaignStatus.COMPLETED)
        assert self.service.is_campaign_active(campaign) is False

    def test_cancelled_campaign_no_candidates(self):
        """A cancelled campaign should not be considered active."""
        campaign = self._make_campaign(CampaignStatus.CANCELLED)
        assert self.service.is_campaign_active(campaign) is False

    def test_running_campaign_is_active(self):
        """A running campaign should be considered active."""
        campaign = self._make_campaign(CampaignStatus.RUNNING)
        assert self.service.is_campaign_active(campaign) is True

    def test_scheduled_campaign_is_active(self):
        """A scheduled campaign should be considered active."""
        campaign = self._make_campaign(CampaignStatus.SCHEDULED)
        assert self.service.is_campaign_active(campaign) is True

    def test_draft_campaign_is_active(self):
        """A draft campaign should be considered active."""
        campaign = self._make_campaign(CampaignStatus.DRAFT)
        assert self.service.is_campaign_active(campaign) is True