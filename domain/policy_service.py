"""
domain/policy_service.py — Business-configurable reminder + SMS policy
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Business owners can configure:
  - Reminder schedule (which days before due/appointment date)
  - Quiet hours (no sends between X and Y — e.g., 21:00–07:00)
  - Business hours (separate from quiet hours — messages scheduled
    outside business hours are deferred to next business day)
  - Max reminder attempts per transaction
  - Tone variant (professional, friendly, urgent)
  - Late notice cadence (daily, every 3 days, weekly)
  - Daily SMS budget cap in KES (cost control — each SMS costs ~KES 1)
  - Max promo messages per customer per week (Kenya SMS guidelines: 3)

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - "School" → "Business", "director" → "owner"
  - NEW fields: business_hours_start, business_hours_end (default 7–19)
  - NEW field: daily_sms_budget_kes (default 500 — small business budget)
  - NEW field: max_promo_per_week (default 3 — Kenya CA guideline)
  - Quiet hours default shifted to 20:00–07:00 (Kenya marketing guideline)

INHERITED LOGIC
---------------
  - JSON storage in `businesses.reminder_policy` (flexible, no migration
    needed for new fields).
  - Policy changes are logged to audit_events.
  - Pydantic validation catches invalid policies at save time.
  - `is_quiet_hours()` handles wrap-around midnight (e.g., 21:00–08:00).

TEACHING NOTES
--------------
  - Quiet hours = "never send at these times" (hard block).
  - Business hours = "prefer to send during these times" (soft block;
    transactional messages like payment confirmations can still send
    outside business hours, but promotional messages are deferred).
  - The daily SMS budget is enforced by the send worker: it sums the
    cost of messages sent today and pauses when the budget is exceeded.
  - `max_promo_per_week` is enforced by the campaign service.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - Scheduler (workers/reminders.py) loads policy to get the schedule dict.
  - Send worker (workers/sends.py) checks `is_quiet_hours()` and
    `is_business_hours()` before sending.
  - Campaign service (domain/campaign_service.py) checks `max_promo_per_week`.
  - `infra/audit_logger.py` records policy changes.
═══════════════════════════════════════════════════════════════════════
"""

import json
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from domain.models import ReminderType
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from sqlalchemy import select


class ReminderSchedule(BaseModel):
    """Days before due/appointment date for each reminder type."""
    due_14: int = Field(default=14, ge=0, le=365)
    due_3: int = Field(default=3, ge=0, le=365)
    due_today: int = Field(default=0, ge=0, le=365)


class QuietHours(BaseModel):
    """
    Hard block: no SMS sent during these hours.
    Default 20:00–07:00 per Kenya Communications Authority marketing guideline.
    Transactional messages (payment confirmations) are exempt in Phase 2.
    """
    start_hour: int = Field(default=20, ge=0, le=23)
    end_hour: int = Field(default=7, ge=0, le=23)


class BusinessHours(BaseModel):
    """
    Soft block: promotional messages deferred to next business day if
    scheduled outside these hours. Transactional messages still send.

    Default 07:00–19:00 (typical Kenyan small business hours).
    """
    start_hour: int = Field(default=7, ge=0, le=23)
    end_hour: int = Field(default=19, ge=0, le=23)


class ReminderPolicy(BaseModel):
    """
    Validated reminder + SMS policy for a business.

    Stored as JSON in `businesses.reminder_policy`.
    """
    version: str = Field(default="v1")
    schedule: ReminderSchedule = Field(default_factory=ReminderSchedule)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    # ── NEW: business hours (separate from quiet hours) ──
    business_hours: BusinessHours = Field(default_factory=BusinessHours)
    max_reminder_attempts: int = Field(default=3, ge=1, le=20)
    tone_variant: str = Field(default="professional")  # professional, friendly, urgent
    late_notice_cadence_days: int = Field(default=7, ge=1, le=30)
    max_sms_segments: int = Field(default=2, ge=1, le=5)
    enabled: bool = Field(default=True)
    # ── NEW: daily SMS budget in KES (cost control) ──
    daily_sms_budget_kes: int = Field(default=500, ge=0, le=100000)
    # ── NEW: max promotional SMS per customer per week (Kenya CA: 3) ──
    max_promo_per_week: int = Field(default=3, ge=0, le=10)

    @field_validator("tone_variant")
    @classmethod
    def validate_tone(cls, v: str) -> str:
        allowed = {"professional", "friendly", "urgent"}
        if v not in allowed:
            raise ValueError(f"tone_variant must be one of {allowed}")
        return v


class PolicyService:
    """
    Load, validate, and update business reminder + SMS policies.
    """

    @staticmethod
    def _cache_key(business_id: int) -> str:
        """
        E6: Redis cache key for business policy.

        Format: ``business:{id}:policy``
        TTL: 5 minutes (300 seconds)

        The cache is invalidated on save_policy() by deleting this key.
        """
        return f"business:{business_id}:policy"

    async def load_policy(self, business_id: int) -> ReminderPolicy:
        """
        Load policy from business record. Returns default if not set.

        E6: Checks Redis cache first (5-minute TTL). Falls back to DB
        on cache miss and populates the cache for subsequent calls.

        TEACHING NOTE: Returning a default (not None) means the scheduler
        always has a valid policy to work with — no None checks needed
        downstream.
        """
        from infra.redis_pool import get_redis_client

        # E6: Try Redis cache first
        try:
            redis = get_redis_client()
            cached = await redis.get(self._cache_key(business_id))
            if cached:
                return ReminderPolicy.model_validate_json(cached)
        except Exception:
            # Redis might be down — fall through to DB query.
            pass

        # Cache miss (or Redis down) → load from DB
        async with async_session_factory() as session:
            from domain.models import Business
            result = await session.execute(select(Business).where(Business.id == business_id))
            business = result.scalar_one_or_none()
            if business and business.reminder_policy:
                data = json.loads(business.reminder_policy)
                policy = ReminderPolicy(**data)
            else:
                policy = ReminderPolicy()

        # E6: Populate the Redis cache for subsequent calls (5-min TTL)
        try:
            redis = get_redis_client()
            await redis.set(
                self._cache_key(business_id),
                policy.model_dump_json(),
                ex=300,  # 5 minutes
            )
        except Exception:
            pass  # caching is a performance optimization, not critical

        return policy

    async def save_policy(
        self,
        business_id: int,
        policy: ReminderPolicy,
        changed_by: str = "owner",
    ) -> None:
        """
        Save policy to business record and log audit event.

        The audit event captures old + new policy JSON for compliance
        (Kenya DPA 2019: all data handling changes must be logged).
        """
        async with async_session_factory() as session:
            from domain.models import Business
            result = await session.execute(select(Business).where(Business.id == business_id))
            business = result.scalar_one_or_none()
            if not business:
                raise ValueError(f"Business {business_id} not found")

            old_policy = business.reminder_policy
            business.reminder_policy = policy.model_dump_json()
            await session.commit()

            await log_audit_event(
                event_type="policy.changed",
                entity_type="business",
                entity_id=str(business_id),
                summary=f"Reminder policy updated by {changed_by}",
                details=json.dumps({
                    "old": old_policy,
                    "new": business.reminder_policy,
                }),
                context=AuditContext(business_id=business_id, actor_type="user", actor_id=changed_by),
            )

        # E6: Invalidate the Redis cache after policy update.
        try:
            from infra.redis_pool import get_redis_client
            redis = get_redis_client()
            await redis.delete(self._cache_key(business_id))
        except Exception:
            pass  # cache invalidation is best-effort

    def schedule_to_dict(self, policy: ReminderPolicy) -> dict[ReminderType, int]:
        """
        Convert policy schedule to dict used by ReminderService.
        """
        return {
            ReminderType.DUE_14: policy.schedule.due_14,
            ReminderType.DUE_3: policy.schedule.due_3,
            ReminderType.DUE_TODAY: policy.schedule.due_today,
        }

    def is_quiet_hours(self, policy: ReminderPolicy, hour: int) -> bool:
        """
        Check if given hour falls in quiet hours (hard block).
        Handles wrap-around (e.g., 20:00–07:00 crosses midnight).
        """
        start = policy.quiet_hours.start_hour
        end = policy.quiet_hours.end_hour
        if start < end:
            return start <= hour < end
        else:
            return hour >= start or hour < end

    def is_business_hours(self, policy: ReminderPolicy, hour: int) -> bool:
        """
        Check if given hour falls within business hours (soft block).
        Handles wrap-around (e.g., 19:00–07:00 means "closed overnight").

        TEACHING NOTE: Business hours are the INVERSE of quiet hours in
        many cases, but they're configurable separately because:
          - A clinic may have quiet hours 20:00–07:00 but business hours
            08:00–17:00 (the gap 17:00–20:00 allows transactional SMS
            but defers promotional SMS).
          - A bar may have business hours 16:00–02:00 (crosses midnight).
        """
        start = policy.business_hours.start_hour
        end = policy.business_hours.end_hour
        if start < end:
            return start <= hour < end
        else:
            return hour >= start or hour < end