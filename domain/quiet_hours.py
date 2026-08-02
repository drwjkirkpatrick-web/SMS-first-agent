"""
domain/quiet_hours.py — Quiet hours enforcement for the send worker
═══════════════════════════════════════════════════

Prevents SMS messages from being sent during configured quiet hours
(e.g., 20:00–07:00 local time per Kenya Communications Authority
marketing guideline). Messages that would be sent during quiet hours
are deferred to the next allowed send time.

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - School → Business, school_id → business_id, schools → businesses
  - Default timezone is Africa/Nairobi (EAT, UTC+3), NOT
    America/Los_Angeles. Kenya has NO daylight saving time, which
    simplifies the wrap-around logic (offsets never shift mid-window).
  - OutboundMessage in this codebase has `business_id` (NOT school_id),
    so defer_if_quiet_hours reads message.business_id when building
    the AuditContext.
  - Default quiet-hours window shifted from 21:00–08:00 (US) to
    20:00–07:00 (Kenya CA marketing guideline). The defaults live in
    domain/policy_service.py::QuietHours; this module reads whatever
    the policy says.
  - AuditContext keeps the legacy field name `school_id` (defined in
    infra/audit_logger.py) but it carries the business_id in practice;
    we pass message.business_id into that field.

The send worker calls defer_if_quiet_hours() before dispatching each
message. If the current time falls within quiet hours, the message's
scheduled_at is updated to the next allowed time and the worker skips
it on this poll cycle.

TIMEZONE HANDLING
-----------------
  - All comparisons are done in the business's local timezone.
  - We use zoneinfo.ZoneInfo (Python 3.9+) for IANA timezone support.
  - The `now` parameter should be a timezone-aware UTC datetime;
    it is converted to the business's local time internally.
  - Default business timezone is "Africa/Nairobi" (set in
    models.py::Business.timezone).

TEACHING NOTES
--------------
  - Quiet hours can wrap around midnight (e.g., 20:00–07:00).
    is_within_quiet_hours handles this case correctly.
  - next_allowed_send_time returns a UTC datetime (timezone-aware)
    so it can be stored directly in OutboundMessage.scheduled_at.
  - The policy's quiet_hours.start_hour and end_hour are integers
    (0–23). A policy with start == end means no quiet hours (always
    allowed).
  - Kenya has no DST, so the Africa/Nairobi offset (UTC+3) is stable
    year-round — unlike America/Los_Angeles, where the PST/PDT shift
    could change which wall-clock hour a UTC instant maps to.
  - The first two methods (is_within_quiet_hours, next_allowed_send_time)
    are pure (no I/O) and can be unit-tested without a database.
    defer_if_quiet_hours() combines them with a session update for
    use by the send worker.
═══════════════════════════════════════════════════
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import AuditEventType, OutboundMessage
from domain.policy_service import ReminderPolicy
from infra.audit_logger import AuditContext, log_audit_event


# Default IANA timezone for Kenyan small businesses. Each Business row
# stores its own timezone (Business.timezone, default "Africa/Nairobi"),
# and callers pass that tz_name in — but we keep the default here for
# documentation and as a fallback if a caller omits it.
_DEFAULT_TZ = "Africa/Nairobi"


class QuietHoursService:
    """
    Enforces quiet hours for outbound SMS messages.

    The first two methods are pure (no I/O) and can be unit-tested
    without a database. defer_if_quiet_hours() combines them with
    a session update for use by the send worker.
    """

    def is_within_quiet_hours(
        self,
        policy: ReminderPolicy,
        now: datetime,
        tz_name: str,
    ) -> bool:
        """
        Check if the given moment falls within the policy's quiet hours.

        Converts `now` (UTC) to the business's local timezone, then checks
        whether the local hour is within the quiet-hours window.

        Handles wrap-around: if start_hour > end_hour (e.g., 20→07),
        the quiet period crosses midnight.

        If start_hour == end_hour, quiet hours are disabled (returns False).

        Args:
            policy: the business's ReminderPolicy (contains quiet_hours config)
            now: timezone-aware UTC datetime to check
            tz_name: IANA timezone name (e.g., "Africa/Nairobi")

        Returns:
            True if the current local time is within quiet hours
        """
        start = policy.quiet_hours.start_hour
        end = policy.quiet_hours.end_hour

        # No quiet hours configured (start == end)
        if start == end:
            return False

        # Convert to business's local time
        local_now = self._to_local(now, tz_name)
        hour = local_now.hour

        # Wrap-around case: e.g., 20:00–07:00 (crosses midnight)
        if start > end:
            return hour >= start or hour < end

        # Normal case: e.g., 12:00–14:00
        return start <= hour < end

    def next_allowed_send_time(
        self,
        policy: ReminderPolicy,
        now: datetime,
        tz_name: str,
    ) -> datetime:
        """
        Compute the next datetime (in UTC) after quiet hours end.

        If currently within quiet hours, returns the UTC datetime
        corresponding to the next end_hour in the business's local timezone.
        If NOT within quiet hours, returns `now` unchanged (send is allowed
        immediately).

        Args:
            policy: the business's ReminderPolicy
            now: timezone-aware UTC datetime
            tz_name: IANA timezone name

        Returns:
            Timezone-aware UTC datetime when sending is next allowed.
            If not in quiet hours, returns `now` as-is.
        """
        if not self.is_within_quiet_hours(policy, now, tz_name):
            return now

        end_hour = policy.quiet_hours.end_hour
        local_now = self._to_local(now, tz_name)

        # Construct today's end-of-quiet-hours datetime in local time
        # at the start of the end_hour (e.g., 07:00:00 for a 20:00–07:00 window)
        local_end = local_now.replace(
            hour=end_hour, minute=0, second=0, microsecond=0
        )

        # If the local end time has already passed today (can happen in
        # wrap-around when we're past midnight but before end_hour —
        # e.g., it's 02:00 and end is 07:00, so today's 07:00 is correct),
        # we need to check: is local_end still in the future?
        if local_end <= local_now:
            # Today's end_hour already passed — advance to tomorrow
            local_end += timedelta(days=1)

        # Convert back to UTC
        return local_end.astimezone(dt_timezone.utc)

    async def defer_if_quiet_hours(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        policy: ReminderPolicy,
        tz_name: str,
    ) -> bool:
        """
        If the current time is within quiet hours, defer the message.

        Updates message.scheduled_at to the next allowed send time
        (in UTC) and flushes the change. The send worker should skip
        the message on this poll cycle; it will be picked up on the
        next poll after quiet hours end.

        If NOT in quiet hours, does nothing and returns False.

        Args:
            session: active async DB session
            message: the OutboundMessage to potentially defer.
                     In this codebase OutboundMessage has `business_id`
                     (not school_id).
            policy: the business's ReminderPolicy
            tz_name: IANA timezone name for the business
                     (e.g., "Africa/Nairobi")

        Returns:
            True if the message was deferred (in quiet hours),
            False if it can be sent now
        """
        now = datetime.now(dt_timezone.utc)

        if not self.is_within_quiet_hours(policy, now, tz_name):
            return False

        next_time = self.next_allowed_send_time(policy, now, tz_name)

        # Update the message's scheduled time to the next allowed window
        message.scheduled_at = next_time
        await session.flush()

        # Audit the deferral
        # AuditContext.school_id is the legacy field name (defined in
        # infra/audit_logger.py); it carries the business_id in practice.
        # OutboundMessage.business_id is the column in this codebase.
        await log_audit_event(
            event_type=AuditEventType.REMINDER_SUPPRESSED,
            entity_type="message",
            entity_id=message.message_key,
            summary=(
                f"Message deferred to {next_time.isoformat()} due to quiet hours "
                f"({tz_name})"
            ),
            context=AuditContext(
                school_id=message.business_id,  # legacy field name; carries business_id
                actor_type="worker",
                actor_id="quiet_hours_service",
            ),
        )

        return True

    # ── Internal helpers ──

    @staticmethod
    def _to_local(now: datetime, tz_name: str) -> datetime:
        """
        Convert a UTC datetime to the given IANA timezone.

        If `now` is naive (no tzinfo), assume UTC.
        """
        if now.tzinfo is None:
            now = now.replace(tzinfo=dt_timezone.utc)
        tz = ZoneInfo(tz_name)
        return now.astimezone(tz)