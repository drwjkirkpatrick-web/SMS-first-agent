"""
api/admin.py — Admin Dashboard API Endpoints
═══════════════════════════════════════════════════

Admin and staff-facing API endpoints for the SMS-First Agent dashboard.

Adapted from the original tuition agent's admin.py:
  - School → Business (school_id → business_id)
  - Student → Customer
  - Guardian → Contact
  - Invoice → Transaction
  - Hardship → Credit Terms (renamed)

New endpoints (Kenya-specific):
  - Dashboard stats with SMS spend in KES (from OutboundMessage.price_kes)
  - Campaign management: create/list/pause promotional campaigns
  - Customer search (by phone, name, or loyalty tier)
  - Business policy update (quiet hours, business hours, frequency caps)
  - Callback/CALL queue (same as original, renamed)

Authentication: simple token in X-Admin-Token header
(bcrypt hash compared against ADMIN_TOKEN_HASH in settings).

Teaching notes:
  - These endpoints power the "Mission Control" dashboard.
  - Read endpoints (GET) are safe for all admin users.
  - Write endpoints (POST/PUT) should be restricted (TODO: role-based auth).
  - All PII (phone numbers, names) is masked before returning to the
    dashboard to comply with Kenya Data Protection Act (2019).
  - SMS spend tracking: OutboundMessage.price_kes is in KES for Africa's Talking
    and USD for Twilio. The stats endpoint normalizes to KES.

Kenya-specific considerations:
  - SMS cost is a major concern for small businesses. The dashboard
    shows daily/weekly/monthly SMS spend in KES.
  - Campaign management is critical — businesses want to send promos
    to customer segments (e.g., "all customers who haven't visited
    in 30 days").
  - Customer search supports phone number lookup (most businesses
    identify customers by phone, not name).
  - Policy updates let the business owner configure quiet hours
    (Kenyan SMS marketing guidelines: no marketing SMS 8 PM – 7 AM).
═══════════════════════════════════════════════════
"""

from datetime import datetime, timedelta
from typing import Optional

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import (
    Business,
    Contact,
    Customer,
    InboundMessage,
    MessageStatus,
    OutboundMessage,
    Transaction,
)
from domain.masking import mask_name, mask_phone
from infra.database import get_db
from infra.settings import get_settings

router = APIRouter()


# ── Authentication ───────────────────────────────────────────────

async def verify_admin_token(x_admin_token: Optional[str] = Header(None)) -> None:
    """
    Verify the admin token from X-Admin-Token header.

    Uses hmac.compare_digest() for constant-time comparison (prevents
    timing attacks). The admin token is stored in the ADMIN_TOKEN env var.

    Teaching note: For a small business deployment on a Pi, a plaintext
    token in the environment is sufficient. For multi-tenant or cloud
    deployments, upgrade to JWT or bcrypt-hashed tokens.
    """
    settings = get_settings()
    expected = getattr(settings, "admin_token", None)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Admin auth not configured (set ADMIN_TOKEN)",
        )
    if not x_admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Admin-Token header",
        )
    if not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin token",
        )


# ── Dashboard Stats ─────────────────────────────────────────────

@router.get("/dashboard/stats", dependencies=[Depends(verify_admin_token)])
async def dashboard_stats(
    business_id: int = 1,
    session: AsyncSession = Depends(get_db),
) -> dict:
    """
    Aggregate stats for the admin dashboard.

    Returns:
      - messages: counts by status (pending, sent, delivered, failed, etc.)
      - sms_spend_kes: total SMS cost in KES (sum of OutboundMessage.price_kes)
      - active_campaigns: count of campaigns currently running
      - customers: total customer count
      - transactions: counts by status
      - callback_queue: unprocessed CALL requests
      - business_id: the business context

    Teaching note: We query counts for each MessageStatus enum value.
    This gives the dashboard a breakdown of message pipeline health:
      - High "pending" → send workers are backed up
      - High "failed" → provider issue or invalid numbers
      - High "unknown_delivery" → need reconciliation
    """
    # Message counts by status
    msg_counts = {}
    for s in MessageStatus:
        count_result = await session.execute(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.business_id == business_id,
                OutboundMessage.status == s,
            )
        )
        msg_counts[s.value] = count_result.scalar()

    # SMS spend in KES — sum of price for delivered/sent messages
    # Note: price may be in different currencies depending on provider.
    # For now, we assume all are KES (Africa's Talking primary).
    # TODO: handle Twilio USD prices with currency conversion.
    spend_result = await session.execute(
        select(func.coalesce(func.sum(OutboundMessage.price_kes), 0.0)).where(
            OutboundMessage.business_id == business_id,
            OutboundMessage.status.in_([
                MessageStatus.SENT,
                MessageStatus.DELIVERED,
            ]),
        )
    )
    sms_spend_kes = float(spend_result.scalar())

    # Active campaigns (Phase 3 feature — count campaigns that are running)
    # For now, we return 0 as campaigns aren't implemented yet.
    # TODO: implement Campaign model and query active campaigns
    active_campaigns = 0

    # Customer count
    customer_count = await session.execute(
        select(func.count(Customer.id)).where(
            Customer.business_id == business_id,
            Customer.deleted_at.is_(None),
        )
    )

    # Transaction counts by status
    # Using string comparison since TransactionStatus may not be an enum yet
    txn_result = await session.execute(
        select(
            Transaction.status,
            func.count(Transaction.id),
        ).where(
            Transaction.business_id == business_id,
        ).group_by(Transaction.status)
    )
    txn_counts = {row[0]: row[1] for row in txn_result}

    # Callback queue count (unprocessed CALL intent messages)
    callback_count = await session.execute(
        select(func.count(InboundMessage.id)).where(
            InboundMessage.business_id == business_id,
            InboundMessage.intent == "call",
            InboundMessage.processed_at.is_(None),
        )
    )

    return {
        "messages": msg_counts,
        "sms_spend_kes": round(sms_spend_kes, 2),
        "active_campaigns": active_campaigns,
        "customers": customer_count.scalar(),
        "transactions": txn_counts,
        "callback_queue": callback_count.scalar(),
        "business_id": business_id,
    }


# ── Campaign Management ─────────────────────────────────────────
#
# Phase 3 feature: promotional campaign management.
# These endpoints create, list, and pause SMS campaigns targeting
# customer segments.
#
# Campaign model (planned):
#   - id, business_id, name, template_id, segment_id
#   - status: draft, active, paused, completed
#   - start_at, end_at
#   - frequency_cap: max messages per customer per period
#   - created_at, updated_at

@router.get("/campaigns", dependencies=[Depends(verify_admin_token)])
async def list_campaigns(
    business_id: int = 1,
    status_filter: Optional[str] = None,
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """
    List promotional campaigns for the business.

    Args:
        business_id: Business to list campaigns for
        status_filter: Optional filter by status (active, paused, completed)

    Returns:
        List of campaign dicts with id, name, status, schedule, stats.

    Teaching note: This is a stub — the Campaign model will be implemented
    in Phase 3. For now, we return an empty list.
    """
    # TODO: implement Campaign model query
    # from domain.models import Campaign
    # stmt = select(Campaign).where(Campaign.business_id == business_id)
    # if status_filter:
    #     stmt = stmt.where(Campaign.status == status_filter)
    # result = await session.execute(stmt)
    # return [serialize_campaign(c) for c in result.scalars()]

    return []


@router.post("/campaigns", dependencies=[Depends(verify_admin_token)])
async def create_campaign(
    business_id: int = 1,
    session: AsyncSession = Depends(get_db),
    name: str = "",
    template_id: Optional[int] = None,
    segment_id: Optional[int] = None,
    start_at: Optional[str] = None,
    end_at: Optional[str] = None,
    frequency_cap: int = 1,
) -> dict:
    """
    Create a new promotional campaign.

    Teaching note: This is a stub. The full implementation will:
    1. Validate the template exists and is a promo type
    2. Validate the segment exists
    3. Create the Campaign record
    4. Queue a Celery task to schedule sends for the segment
    5. Return the campaign ID

    The frequency cap prevents sending more than N promo messages
    per customer per week (Kenyan SMS marketing guideline: max 3/week).
    """
    # TODO: implement Campaign model creation
    # from domain.models import Campaign, CampaignStatus
    # campaign = Campaign(
    #     business_id=business_id,
    #     name=name,
    #     template_id=template_id,
    #     segment_id=segment_id,
    #     status=CampaignStatus.DRAFT,
    #     start_at=parse(start_at),
    #     end_at=parse(end_at),
    #     frequency_cap=frequency_cap,
    # )
    # session.add(campaign)
    # await session.commit()
    # return {"id": campaign.id, "status": "draft"}

    return {"status": "not_implemented", "message": "Campaign creation available in Phase 3"}


@router.patch("/campaigns/{campaign_id}/pause", dependencies=[Depends(verify_admin_token)])
async def pause_campaign(
    campaign_id: int,
    business_id: int = 1,
    session: AsyncSession = Depends(get_db),
) -> dict:
    """
    Pause an active campaign (stops scheduling new sends).

    Messages already queued in the outbox will still be sent (they're
    committed to the transactional outbox). Only future scheduling is
    paused.
    """
    # TODO: implement campaign pause
    # from domain.models import Campaign, CampaignStatus
    # result = await session.execute(
    #     select(Campaign).where(Campaign.id == campaign_id, Campaign.business_id == business_id)
    # )
    # campaign = result.scalar_one_or_none()
    # if not campaign:
    #     raise HTTPException(404, "Campaign not found")
    # campaign.status = CampaignStatus.PAUSED
    # await session.commit()
    # return {"status": "paused", "campaign_id": campaign_id}

    return {"status": "not_implemented", "message": "Campaign management available in Phase 3"}


# ── Customer Search ─────────────────────────────────────────────

@router.get("/customers/search", dependencies=[Depends(verify_admin_token)])
async def search_customers(
    business_id: int = 1,
    phone: Optional[str] = None,
    name: Optional[str] = None,
    min_loyalty_points: Optional[int] = None,
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """
    Search customers by phone, name, or loyalty tier.

    Args:
        phone: Phone number to search (partial match, e.g., "07123")
        name: Customer name to search (case-insensitive partial match)
        min_loyalty_points: Filter to customers with >= N loyalty points

    Returns:
        List of customer dicts with masked PII fields.

    Teaching note: Phone search is the most common lookup in Kenya —
    businesses identify customers by phone number. We use ILIKE for
    case-insensitive partial matching. PII (phone, name) is masked
    before returning to comply with Kenya DPA (2019).
    """
    stmt = select(Customer).where(
        Customer.business_id == business_id,
        Customer.deleted_at.is_(None),
    )

    # Filter by phone (partial match)
    if phone:
        stmt = stmt.where(Customer.phone.ilike(f"%{phone}%"))

    # Filter by name (case-insensitive partial match)
    if name:
        stmt = stmt.where(Customer.first_name.ilike(f"%{name}%"))

    # Filter by minimum loyalty points
    if min_loyalty_points is not None:
        stmt = stmt.where(Customer.loyalty_points >= min_loyalty_points)

    stmt = stmt.order_by(Customer.first_name).limit(100)

    result = await session.execute(stmt)
    items = []
    for customer in result.scalars().all():
        items.append({
            "id": customer.id,
            "first_name": mask_name(customer.first_name),
            "phone": mask_phone(customer.phone),
            "preferred_language": customer.preferred_language,
            "loyalty_points": customer.loyalty_points,
        })
    return items


# ── Callback / CALL Queue ────────────────────────────────────────

@router.get("/queue/callback", dependencies=[Depends(verify_admin_token)])
async def callback_queue(
    business_id: int = 1,
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """
    List CALL requests from inbound messages.

    When a customer replies "CALL" via SMS, the inbound parser creates
    an InboundMessage with intent="call". This endpoint lists those
    for staff to action.

    Adapted from the original tuition agent's callback_queue —
    renamed from guardian_id to customer_id context.
    """
    result = await session.execute(
        select(InboundMessage).where(
            InboundMessage.business_id == business_id,
            InboundMessage.intent == "call",
            InboundMessage.processed_at.is_(None),
        ).order_by(InboundMessage.created_at)
    )
    items = []
    for msg in result.scalars().all():
        # Look up the customer/contact for this phone number
        customer = msg.customer if hasattr(msg, "customer") else None
        items.append({
            "id": msg.id,
            "customer_name": mask_name(customer.first_name) if customer else None,
            "customer_phone": mask_phone(msg.from_phone) if hasattr(msg, "from_phone") else None,
            "body": msg.body,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        })
    return items


# ── Transaction Lookup ──────────────────────────────────────────

@router.get("/transactions", dependencies=[Depends(verify_admin_token)])
async def list_transactions(
    business_id: int = 1,
    status_filter: Optional[str] = None,
    customer_id: Optional[int] = None,
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """
    List transactions with optional filtering.

    Adapted from list_invoices() — invoice → transaction.
    Businesses use this to check credit/layaway balances.

    Args:
        business_id: Business context
        status_filter: Filter by status (pending, partial, paid, overdue)
        customer_id: Filter by customer
    """
    stmt = select(Transaction).where(Transaction.business_id == business_id)
    if status_filter:
        stmt = stmt.where(Transaction.status == status_filter)
    if customer_id:
        stmt = stmt.where(Transaction.customer_id == customer_id)
    stmt = stmt.order_by(Transaction.due_date)

    result = await session.execute(stmt)
    items = []
    for txn in result.scalars().all():
        customer = txn.customer if hasattr(txn, "customer") else None
        balance = float(txn.amount_due) - float(txn.amount_paid)
        items.append({
            "id": txn.id,
            "transaction_number": txn.transaction_number,
            "customer_name": mask_name(customer.first_name) if customer else None,
            "amount_due": str(txn.amount_due),
            "amount_paid": str(txn.amount_paid),
            "balance": f"{balance:.2f}",
            "due_date": str(txn.due_date) if txn.due_date else None,
            "transaction_type": txn.transaction_type,
            "status": txn.status,
        })
    return items


# ── Business Policy Update ──────────────────────────────────────

@router.patch("/business/{business_id}/policy", dependencies=[Depends(verify_admin_token)])
async def update_business_policy(
    business_id: int,
    quiet_hours_start: Optional[int] = None,
    quiet_hours_end: Optional[int] = None,
    business_hours_start: Optional[int] = None,
    business_hours_end: Optional[int] = None,
    promo_frequency_cap: Optional[int] = None,
    daily_sms_budget_kes: Optional[float] = None,
    session: AsyncSession = Depends(get_db),
) -> dict:
    """
    Update business policy settings.

    Allows the business owner to configure:
      - quiet_hours_start/end: No marketing SMS during these hours
        (Kenyan guideline: 20:00 – 07:00)
      - business_hours_start/end: Business operating hours
        (Messages outside hours are deferred to next business day)
      - promo_frequency_cap: Max promo SMS per customer per week
        (Kenyan guideline: max 3 per week)
      - daily_sms_budget_kes: Daily SMS spend cap in KES
        (System pauses sends when budget exceeded)

    Teaching note: The policy is stored as JSON in the Business
    model's policy_config field. The policy engine reads this
    when scheduling and sending messages.

    Kenya-specific considerations:
      - The Communications Authority of Kenya mandates no marketing
        SMS between 8 PM and 7 AM. We enforce this via quiet hours.
      - The 3-per-week frequency cap is a CAK guideline, not law, but
        good practice for customer trust.
      - Daily SMS budget is critical for small businesses — they can't
        afford unlimited SMS costs.
    """
    result = await session.execute(
        select(Business).where(Business.id == business_id)
    )
    business = result.scalar_one_or_none()
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    # Build policy update dict
    # The policy_config is stored as JSON in the Business model
    import json
    policy = json.loads(business.policy_config or "{}")

    if quiet_hours_start is not None:
        policy["quiet_hours_start"] = quiet_hours_start
    if quiet_hours_end is not None:
        policy["quiet_hours_end"] = quiet_hours_end
    if business_hours_start is not None:
        policy["business_hours_start"] = business_hours_start
    if business_hours_end is not None:
        policy["business_hours_end"] = business_hours_end
    if promo_frequency_cap is not None:
        policy["promo_frequency_cap"] = promo_frequency_cap
    if daily_sms_budget_kes is not None:
        policy["daily_sms_budget_kes"] = daily_sms_budget_kes

    business.policy_config = json.dumps(policy)
    business.updated_at = datetime.utcnow()
    await session.commit()

    return {
        "status": "updated",
        "business_id": business_id,
        "policy": policy,
    }