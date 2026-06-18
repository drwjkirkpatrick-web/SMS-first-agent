"""
workers/campaigns.py — Celery task: process promotional campaigns
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
This worker processes promotional SMS campaigns for Kenyan small
businesses. A campaign is a batch of promotional messages sent to a
customer segment, with frequency capping and deduplication.

Two tasks are defined here:
  1. process_campaign(business_id, campaign_id) — process a specific
     campaign: load it, build candidates with frequency cap enforcement,
     insert into the outbox with campaign-specific message keys.
  2. process_scheduled_campaigns() — periodic task (hourly from Beat)
     that finds SCHEDULED campaigns whose start time has arrived and
     triggers process_campaign for each.

WHAT'S NEW (not in the tuition agent)
-------------------------------------
Schools don't send promotional messages. This is entirely new for the
business context. However, it reuses the EXACT same anti-duplicate
infrastructure:
  - Deterministic message_key (includes campaign_id for dedup)
  - ON CONFLICT DO NOTHING on message_key
  - Transactional outbox insertion (same DispatchService)
  - Send worker picks up campaign messages just like reminders

FREQUENCY CAP LOGIC
-------------------
Kenya Communications Authority guidelines: max 3 marketing SMS per
customer per week. The campaign service enforces this by counting
all promo-type OutboundMessages in the last 7 days for each customer.
If a customer has already received 3 promos this week, they're skipped.

DEDUP KEY FORMAT
----------------
  {business_id}:{campaign_id}:promo:{customer_id}:{send_date}

This means:
  - Re-running the campaign worker on the same day → no duplicates
    (same key, ON CONFLICT DO NOTHING).
  - Running the campaign on different days → different keys → allowed
    (multi-day sales can send daily).
  - Different campaigns to the same customer → different campaign_id
    → different keys → allowed (but frequency cap still applies).

TEACHING NOTES
--------------
  - Campaigns are "fire and forget": once candidates are in the outbox,
    the existing send worker handles delivery, retries, reconciliation.
    The campaign worker doesn't send SMS directly.
  - The frequency cap counts ALL promo-type messages in the last 7 days,
    not just from this campaign. This prevents a customer from receiving
    3 promos from campaign A AND 3 from campaign B in the same week.
  - Quiet hours + business hours are enforced by the send worker, not
    the campaign worker. Promotional messages are deferred outside
    business hours (soft block) and never sent during quiet hours.

KENYA-SPECIFIC CONSIDERATIONS
-----------------------------
  - Kenya CA guideline: max 3 marketing SMS per customer per week.
  - Promotional SMS must include opt-out instructions ("Reply STOP").
  - The promo_message template includes "Reply STOP to opt out" footer.
  - Africa's Talking handles DND (Do Not Disturb) registry compliance
    for Kenyan operators automatically.
═══════════════════════════════════════════════════════════════════════
"""

from datetime import date, datetime

from sqlalchemy import select

from domain.campaign_service import CampaignService
from domain.dispatch_service import DispatchService
from domain.models import (
    Business,
    Campaign,
    CampaignStatus,
    OutboundMessage,
    ReminderType,
)
from domain.reminder_service import ReminderCandidate
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from workers.celery_app import celery_app


# ── Task 1: Process a specific campaign ───────────────────────────


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_campaign(self, business_id: int, campaign_id: int) -> dict:
    """
    Celery task: process a promotional campaign.

    Loads the campaign + segment, builds candidates with frequency cap
    enforcement, inserts into the outbox with campaign-specific
    message_key (includes campaign_id for dedup).

    Args:
        business_id: the business that owns the campaign.
        campaign_id: the campaign to process.

    Returns:
        {"candidates": N, "inserted": M, "suppressed": K, "errors": [...]}
    """
    import asyncio

    return asyncio.run(_async_process_campaign(business_id, campaign_id))


async def _async_process_campaign(business_id: int, campaign_id: int) -> dict:
    campaign_service = CampaignService()
    dispatch_service = DispatchService()

    result: dict = {
        "candidates": 0,
        "inserted": 0,
        "suppressed": 0,
        "duplicates_skipped": 0,
        "errors": [],
    }

    async with async_session_factory() as session:
        try:
            # ── 1. Load the campaign ──────────────────────────────
            camp_result = await session.execute(
                select(Campaign).where(
                    Campaign.id == campaign_id,
                    Campaign.business_id == business_id,
                    Campaign.deleted_at.is_(None),
                )
            )
            campaign = camp_result.scalar_one_or_none()
            if not campaign:
                result["errors"].append(f"Campaign {campaign_id} not found")
                return result

            # Skip if not in SCHEDULED or RUNNING status.
            if campaign.status not in (CampaignStatus.SCHEDULED, CampaignStatus.RUNNING):
                result["errors"].append(
                    f"Campaign {campaign_id} status is {campaign.status.value}, skipping"
                )
                return result

            # Skip if paused or cancelled.
            if campaign.status in (CampaignStatus.PAUSED, CampaignStatus.CANCELLED):
                result["errors"].append(
                    f"Campaign {campaign_id} is {campaign.status.value}, skipping"
                )
                return result

            # ── 2. Build candidates with frequency cap enforcement ──
            # CampaignService.build_campaign_candidates() does:
            #   - Load segment members
            #   - For each member, check frequency cap (max promos/week)
            #   - Check opt-in (skip if opted out)
            #   - Compute dedup message_key
            #   - Return CampaignCandidate list
            candidates = await campaign_service.build_campaign_candidates(
                session=session,
                campaign=campaign,
                send_date=date.today(),
            )
            result["candidates"] = len(candidates)

            # ── 3. Convert CampaignCandidates to ReminderCandidates ──
            # DispatchService.insert_outbox_messages() expects
            # ReminderCandidate objects. We convert CampaignCandidate
            # to ReminderCandidate so the same dispatch path works.
            #
            # TEACHING NOTE: This conversion is deliberate — it means
            # the dispatch service doesn't need to know about campaigns.
            # The outbox is domain-agnostic: a message is a message.
            reminder_candidates = []
            for cc in candidates:
                reminder_candidates.append(ReminderCandidate(
                    business_id=cc.business_id,
                    transaction_id=0,  # campaigns aren't tied to a transaction
                    customer_id=cc.customer_id,
                    contact_id=cc.contact_id,
                    reminder_type=ReminderType.PROMO,
                    due_date=None,  # promos don't have a due date
                    message_key=cc.message_key,
                    body_template=cc.body_template,
                    language=cc.language,
                ))

            # ── 4. Insert into outbox (transactional, idempotent) ──
            # ON CONFLICT DO NOTHING on message_key ensures re-running
            # the campaign worker on the same day is a no-op.
            dispatch_result = await dispatch_service.insert_outbox_messages(
                session=session,
                candidates=reminder_candidates,
            )
            result["inserted"] = dispatch_result["inserted"]
            result["duplicates_skipped"] = dispatch_result["duplicates_skipped"]

            # ── 5. Update campaign status to RUNNING ───────────────
            # build_campaign_candidates() already sets status to RUNNING,
            # but we confirm it here and set the total_candidates counter.
            campaign.total_candidates = len(candidates)
            campaign.status = CampaignStatus.RUNNING

            # ── 6. Audit log ────────────────────────────────────────
            await log_audit_event(
                event_type="campaign.started",
                entity_type="campaign",
                entity_id=str(campaign.id),
                summary=(
                    f"Campaign '{campaign.name}' processed: "
                    f"{len(candidates)} candidates, "
                    f"{dispatch_result['inserted']} inserted"
                ),
                context=AuditContext(
                    business_id=business_id,
                    actor_type="worker",
                    actor_id="campaign_worker",
                ),
            )

            await session.commit()

        except Exception as exc:
            await session.rollback()
            result["errors"].append(str(exc))
            raise

    return result


# ── Task 2: Process scheduled campaigns (periodic) ────────────────


@celery_app.task(bind=True, max_retries=1, default_retry_delay=60)
def process_scheduled_campaigns(self) -> dict:
    """
    Celery task: find SCHEDULED campaigns whose start time has arrived
    and trigger process_campaign for each.

    Runs hourly from Celery Beat. For each business, checks for campaigns
    in SCHEDULED status where schedule_start <= now.

    Returns:
        {"found": N, "triggered": N, "errors": [...]}
    """
    import asyncio

    return asyncio.run(_async_process_scheduled_campaigns())


async def _async_process_scheduled_campaigns() -> dict:
    result: dict = {"found": 0, "triggered": 0, "errors": []}

    async with async_session_factory() as session:
        now = datetime.utcnow()

        # Find campaigns in SCHEDULED status whose start time has arrived.
        camp_result = await session.execute(
            select(Campaign).where(
                Campaign.status == CampaignStatus.SCHEDULED,
                Campaign.schedule_start <= now,
                Campaign.deleted_at.is_(None),
            )
        )
        campaigns = list(camp_result.scalars().all())
        result["found"] = len(campaigns)

        for campaign in campaigns:
            try:
                # Trigger the campaign processing task.
                # We call the async function directly (in a real Celery
                # deployment, this would be .delay() for async dispatch).
                # In this implementation, we process inline for simplicity.
                process_result = await _async_process_campaign(
                    business_id=campaign.business_id,
                    campaign_id=campaign.id,
                )
                if process_result.get("inserted", 0) > 0:
                    result["triggered"] += 1
            except Exception as exc:
                result["errors"].append(
                    f"Campaign {campaign.id}: {str(exc)}"
                )
                continue

        await session.commit()

    return result


# ── Task 3: Complete campaigns (mark as COMPLETED) ────────────────


@celery_app.task(bind=True, max_retries=1, default_retry_delay=60)
def complete_campaign(self, business_id: int, campaign_id: int) -> dict:
    """
    Celery task: mark a campaign as COMPLETED after all messages are sent.

    This is triggered after the send worker has processed all campaign
    candidates. It updates the campaign status and logs the event.

    Returns:
        {"status": "completed", "campaign_id": N}
    """
    import asyncio

    return asyncio.run(_async_complete_campaign(business_id, campaign_id))


async def _async_complete_campaign(business_id: int, campaign_id: int) -> dict:
    campaign_service = CampaignService()

    async with async_session_factory() as session:
        camp_result = await session.execute(
            select(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.business_id == business_id,
            )
        )
        campaign = camp_result.scalar_one_or_none()
        if not campaign:
            return {"status": "error", "reason": "not_found"}

        await campaign_service.complete_campaign(session, campaign)

        await log_audit_event(
            event_type="campaign.completed",
            entity_type="campaign",
            entity_id=str(campaign.id),
            summary=f"Campaign '{campaign.name}' completed: "
                    f"{campaign.total_sent} sent, {campaign.total_suppressed} suppressed",
            context=AuditContext(
                business_id=business_id,
                actor_type="worker",
                actor_id="campaign_worker",
            ),
        )

        await session.commit()

    return {"status": "completed", "campaign_id": campaign_id}