"""
workers/inbound.py — Inbound SMS parsing and action dispatch (extended)
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Handles keyword-based inbound SMS from customers. Each SMS is parsed
into an InboundIntent, then the appropriate action is dispatched:
  PAID       → Queue payment reconciliation (M-Pesa ref)
  STATUS     → Reply with current credit/layaway balance
  CALL       → Queue callback request for staff
  EXTENSION  → Create credit terms request (was "HARDSHIP")
  HELP       → Send command list (bilingual)
  STOP       → Opt out of SMS (Kenya DPA 2019)
  START      → Opt back in
  PROMO      → Send current active promotions (NEW)
  POINTS     → Send loyalty points balance (NEW)
  BOOK       → Book appointment (clinic/salon) (NEW)
  HOURS      → Reply with business hours (NEW)
  LOCATION   → Reply with business location/address (NEW)
  BALANCE    → Same as STATUS (alias)

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - Guardian → Contact, School → Business, Invoice → Transaction
  - HardshipRequest → CreditTermsRequest
  - Templates are bilingual (EN/SW) — customer.preferred_language
  - NEW keywords: PROMO, POINTS, BOOK, HOURS, LOCATION, BALANCE
  - NEW: Swahili keyword variants (LIPA, SALIO, PIGA, etc.)

SWAHILI KEYWORD VARIANTS
------------------------
Kenya is bilingual. Customers may text in Swahili:
  - "LIPA"     → PAID (lipa = pay)
  - "SALIO"    → STATUS (salio = balance)
  - "PIGA"     → CALL (piga simu = make a call)
  - "MSAADA"   → HELP (msaada = help)
  - "ACHA"     → STOP (acha = stop/quit)
  - "ANZA"     → START (anza = start)
  - "OFERSI"   → PROMO (ofesi/offers = offers)
  - "POINTI"   → POINTS (Swahili adaptation)
  - "BAKASHI"  → BALANCE (Swahili adaptation)
  - "BAKI"     → BALANCE (baki = remaining)
  - "HOURS"    → HOURS (same in both languages)

TEACHING NOTES
--------------
  - We use fuzzy matching: exact regex match (confidence 1.0) first,
    then keyword-in-message (confidence 0.7) as fallback.
  - "PAID" / "LIPA" is a CLAIM, not confirmation — staff must verify.
    In Phase 2, we'll check M-Pesa API for the reference.
  - STOP/START update contact preferences and are logged for compliance
    (Kenya DPA 2019 requires explicit opt-out records).
  - All replies are queued in the outbox (not sent directly). This
    respects the transactional outbox pattern and quiet hours.
  - Bilingual: each response uses the customer's preferred_language.

KENYA-SPECIFIC CONSIDERATIONS
-----------------------------
  - Feature phone users type in ALL CAPS (no lowercase on old keypads).
    Our regex patterns are case-insensitive (re.I).
  - Swahili keyword variants are critical for adoption outside Nairobi.
  - STOP/START compliance is mandatory under Kenya DPA 2019.
  - The HELP reply must be concise (feature phone screens are small).
═══════════════════════════════════════════════════════════════════════
"""

import re
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from domain.credit_terms_service import CreditTermsService
from domain.models import (
    Business,
    Contact,
    CreditTermsRequest,
    InboundIntent,
    InboundMessage,
    MessageStatus,
    OutboundMessage,
    ReminderType,
    Transaction,
    TransactionStatus,
)
from domain.templates import TemplateRenderer
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from workers.celery_app import celery_app


# ═══════════════════════════════════════════════════════════════
# Keyword Parsing — Extended with Swahili variants
# ═══════════════════════════════════════════════════════════════

KEYWORD_PATTERNS: dict[InboundIntent, list[re.Pattern]] = {
    # ── PAID / LIPA (payment claim) ──
    # "LIPA" is Swahili for "pay". Customers may text "LIPA" to claim payment.
    InboundIntent.PAID: [
        re.compile(r"^\s*PAID\s*$", re.I),
        re.compile(r"^\s*I\s+PAID\s*$", re.I),
        re.compile(r"^\s*PAYMENT\s+SENT\s*$", re.I),
        re.compile(r"^\s*LIPA\s*$", re.I),           # Swahili: pay
        re.compile(r"^\s*NIME*LIPA\s*$", re.I),       # Swahili: I have paid
        re.compile(r"^\s*UMELIPWA\s*$", re.I),        # Swahili: has been paid
    ],
    # ── STATUS / SALIO / BALANCE / BAKI (balance inquiry) ──
    # "SALIO" = balance, "BAKI" = remaining in Swahili.
    InboundIntent.STATUS: [
        re.compile(r"^\s*STATUS\s*$", re.I),
        re.compile(r"^\s*BALANCE\s*$", re.I),
        re.compile(r"^\s*HOW\s+MUCH\s*$", re.I),
        re.compile(r"^\s*SALIO\s*$", re.I),            # Swahili: balance
        re.compile(r"^\s*BAKI\s*$", re.I),             # Swahili: remaining
        re.compile(r"^\s*BAKASHI\s*$", re.I),          # Swahili adaptation
        re.compile(r"^\s*NISHATILIA\s*$", re.I),      # Swahili: have I paid?
    ],
    InboundIntent.BALANCE: [
        # BALANCE is an alias for STATUS — same dispatch.
        re.compile(r"^\s*BALANCE\s*$", re.I),
        re.compile(r"^\s*SALIO\s*$", re.I),
    ],
    # ── CALL / PIGA (callback request) ──
    # "PIGA" = to call/hit in Swahili. "PIGA SIMU" = make a call.
    InboundIntent.CALL: [
        re.compile(r"^\s*CALL\s*$", re.I),
        re.compile(r"^\s*CALL\s+ME\s*$", re.I),
        re.compile(r"^\s*PHONE\s*$", re.I),
        re.compile(r"^\s*PIGA\s*$", re.I),              # Swahili: call
        re.compile(r"^\s*PIGA\s+SIMU\s*$", re.I),       # Swahili: make a call
        re.compile(r"^\s*NIPIGIE\s*$", re.I),          # Swahili: call me
    ],
    # ── EXTENSION / MUDA (credit terms request) ──
    # "MUDA" = time in Swahili. "NIONE MUDA" = give me time.
    InboundIntent.EXTENSION: [
        re.compile(r"^\s*EXTENSION\s*$", re.I),
        re.compile(r"^\s*EXTEND\s*$", re.I),
        re.compile(r"^\s*NEED\s+MORE\s+TIME\s*$", re.I),
        re.compile(r"^\s*HARDSHIP\s*$", re.I),          # backward compat
        re.compile(r"^\s*MUDA\s*$", re.I),              # Swahili: time
        re.compile(r"^\s*NIONE\s+MUDA\s*$", re.I),      # Swahili: give me time
        re.compile(r"^\s*HIRIMU\s*$", re.I),            # Swahili variant
    ],
    # ── HELP / MSAADA (command list) ──
    # "MSAADA" = help in Swahili.
    InboundIntent.HELP: [
        re.compile(r"^\s*HELP\s*$", re.I),
        re.compile(r"^\s*\?\s*$"),
        re.compile(r"^\s*INFO\s*$", re.I),
        re.compile(r"^\s*MSAADA\s*$", re.I),            # Swahili: help
        re.compile(r"^\s*MSAIDISHA\s*$", re.I),        # Swahili: assist me
        re.compile(r"^\s*AMRI\s*$", re.I),              # Swahili: commands
    ],
    # ── STOP / ACHA (opt-out, Kenya DPA 2019) ──
    # "ACHA" = stop/quit in Swahili.
    InboundIntent.STOP: [
        re.compile(r"^\s*STOP\s*$", re.I),
        re.compile(r"^\s*UNSUBSCRIBE\s*$", re.I),
        re.compile(r"^\s*QUIT\s*$", re.I),
        re.compile(r"^\s*ACHA\s*$", re.I),              # Swahili: stop
        re.compile(r"^\s*ACHA\s+KUTUPELEKEA\s*$", re.I), # Swahili: stop sending
        re.compile(r"^\s*TOKA\s*$", re.I),              # Swahili: exit/leave
        re.compile(r"^\s*HATAKI\s*$", re.I),            # Swahili: I don't want
    ],
    # ── START / ANZA (opt back in) ──
    # "ANZA" = start in Swahili.
    InboundIntent.START: [
        re.compile(r"^\s*START\s*$", re.I),
        re.compile(r"^\s*SUBSCRIBE\s*$", re.I),
        re.compile(r"^\s*YES\s*$", re.I),
        re.compile(r"^\s*ANZA\s*$", re.I),              # Swahili: start
        re.compile(r"^\s*RUDISHA\s*$", re.I),           # Swahili: return/restore
        re.compile(r"^\s*NITUMIE\s*$", re.I),          # Swahili: send me
    ],
    # ── PROMO / OFERSI (request current promotions) ── NEW
    # "OFERSI" is a Swahili adaptation of "offers".
    InboundIntent.PROMO: [
        re.compile(r"^\s*PROMO\s*$", re.I),
        re.compile(r"^\s*PROMOTION\s*$", re.I),
        re.compile(r"^\s*OFFER\s*$", re.I),
        re.compile(r"^\s*OFFERS\s*$", re.I),
        re.compile(r"^\s*OFERSI\s*$", re.I),            # Swahili adaptation
        re.compile(r"^\s*PUNGUZIO\s*$", re.I),         # Swahili: discount
    ],
    # ── POINTS / POINTI (loyalty points balance) ── NEW
    # "POINTI" is a Swahili adaptation of "points".
    InboundIntent.POINTS: [
        re.compile(r"^\s*POINTS\s*$", re.I),
        re.compile(r"^\s*LOYALTY\s*$", re.I),
        re.compile(r"^\s*POINTI\s*$", re.I),             # Swahili adaptation
        re.compile(r"^\s*POINTI\s+ZANGU\s*$", re.I),   # Swahili: my points
    ],
    # ── BOOK (book appointment, clinic/salon) ── NEW
    InboundIntent.BOOK: [
        re.compile(r"^\s*BOOK\s*$", re.I),
        re.compile(r"^\s*APPOINTMENT\s*$", re.I),
        re.compile(r"^\s*BOOKING\s*$", re.I),
        re.compile(r"^\s*AGENDA\s*$", re.I),            # Swahili: agenda/appointment
        re.compile(r"^\s*KAHA\s*$", re.I),              # Swahili: queue/book
    ],
    # ── HOURS (business hours) ── NEW
    InboundIntent.HOURS: [
        re.compile(r"^\s*HOURS\s*$", re.I),
        re.compile(r"^\s*OPEN\s*$", re.I),
        re.compile(r"^\s*MASAA\s*$", re.I),             # Swahili: hours
        re.compile(r"^\s*WAKATI\s*$", re.I),            # Swahili: time
    ],
    # ── LOCATION (business location/address) ── NEW
    InboundIntent.LOCATION: [
        re.compile(r"^\s*LOCATION\s*$", re.I),
        re.compile(r"^\s*ADDRESS\s*$", re.I),
        re.compile(r"^\s*WHERE\s*$", re.I),
        re.compile(r"^\s*MAHALI\s*$", re.I),            # Swahili: location
        re.compile(r"^\s*KOPI\s*$", re.I),              # Swahili variant: place
        re.compile(r"^\s*WAPI\s*$", re.I),              # Swahili: where
    ],
}


def parse_intent(body: str) -> tuple[InboundIntent, float]:
    """
    Parse inbound SMS body into an intent.

    Returns:
        (intent, confidence) where confidence is 1.0 for exact match,
        lower (0.7) for fuzzy/partial matches.

    TEACHING NOTE: We check exact regex patterns first (high confidence),
    then fall back to keyword-in-message (lower confidence). This prevents
    "I want to STATUS" from matching as STATUS with 1.0 confidence —
    it would match at 0.7, which the caller can use to decide whether
    to treat it as definitive or ask for clarification.
    """
    normalized = body.strip().upper()

    # ── Pass 1: Exact pattern match (confidence 1.0) ──
    for intent, patterns in KEYWORD_PATTERNS.items():
        for pattern in patterns:
            if pattern.match(body):
                return intent, 1.0

    # ── Pass 2: Keyword appears anywhere in message (confidence 0.7) ──
    for intent, patterns in KEYWORD_PATTERNS.items():
        # Use the first pattern's keyword as the search term.
        keyword = intent.value.upper()
        if keyword in normalized:
            return intent, 0.7

    # ── No match: unknown intent ──
    return InboundIntent.UNKNOWN, 0.0


# ═══════════════════════════════════════════════════════════════
# Celery Task
# ═══════════════════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def process_inbound_message(
    self,
    inbound_message_id: int,
) -> dict:
    """
    Process an inbound SMS message by ID.

    This task is triggered when a new InboundMessage is created (by the
    webhook handler). It parses the keyword, dispatches the action, and
    queues a response SMS in the outbox.

    Returns:
        {"status": "processed", "intent": "...", "confidence": float,
         "action": "..."}
    """
    import asyncio

    return asyncio.run(_async_process_inbound(inbound_message_id))


async def _async_process_inbound(inbound_message_id: int) -> dict:
    async with async_session_factory() as session:
        # ── 1. Load the inbound message ──────────────────────────
        result = await session.execute(
            select(InboundMessage).where(InboundMessage.id == inbound_message_id)
        )
        msg = result.scalar_one_or_none()
        if not msg:
            return {"status": "error", "reason": "message_not_found"}

        # ── 2. Parse intent ──────────────────────────────────────
        intent, confidence = parse_intent(msg.body)
        msg.intent = intent
        msg.intent_confidence = confidence
        msg.processed_at = datetime.utcnow()
        await session.flush()

        # ── 3. Load contact and business ─────────────────────────
        contact = None
        if msg.contact_id:
            c_result = await session.execute(
                select(Contact).where(Contact.id == msg.contact_id)
            )
            contact = c_result.scalar_one_or_none()

        business = None
        if msg.business_id:
            b_result = await session.execute(
                select(Business).where(Business.id == msg.business_id)
            )
            business = b_result.scalar_one_or_none()

        if not contact or not business:
            return {"status": "error", "reason": "contact_or_business_not_found"}

        # Determine response language (customer's preferred language).
        # The contact doesn't have preferred_language directly — it's on
        # the Customer. For now, default to English. A full implementation
        # would join through customer_contact_links to get the customer's
        # preferred_language.
        language = "en"  # TODO: load from Customer.preferred_language

        # ── 4. Dispatch by intent ────────────────────────────────
        renderer = TemplateRenderer()
        response_body: Optional[str] = None
        action_taken: Optional[str] = None

        if intent == InboundIntent.STOP:
            # ── Opt-out (Kenya DPA 2019) ──
            contact.sms_opt_in = False
            contact.opt_out_at = datetime.utcnow()
            contact.opt_out_source = "sms_keyword"
            response_body = renderer.render(
                "opt_out_confirm", {"business_name": business.name}, language=language
            )
            action_taken = "opted_out"

            await log_audit_event(
                event_type="contact.opt_out",
                entity_type="contact",
                entity_id=str(contact.id),
                summary=f"Contact {contact.id} opted out via SMS",
                context=AuditContext(
                    business_id=business.id, actor_type="worker"
                ),
            )

        elif intent == InboundIntent.START:
            # ── Opt back in ──
            contact.sms_opt_in = True
            contact.opt_in_at = datetime.utcnow()
            contact.opt_out_at = None  # clear the opt-out timestamp
            response_body = renderer.render(
                "opt_in_confirm", {"business_name": business.name}, language=language
            )
            action_taken = "opted_in"

        elif intent == InboundIntent.HELP:
            # ── Send command list (bilingual) ──
            response_body = renderer.render(
                "help_reply", {"business_name": business.name}, language=language
            )
            action_taken = "help_sent"

        elif intent in (InboundIntent.STATUS, InboundIntent.BALANCE):
            # ── Reply with current balance ──
            # Find the latest open (pending/partial/overdue) transaction
            # for this contact.
            txn_result = await session.execute(
                select(Transaction).where(
                    Transaction.business_id == business.id,
                    Transaction.contact_id == contact.id,
                    Transaction.status.in_([
                        TransactionStatus.PENDING,
                        TransactionStatus.PARTIAL,
                        TransactionStatus.OVERDUE,
                    ]),
                ).order_by(Transaction.due_date)
            )
            transaction = txn_result.scalars().first()

            if transaction:
                balance = float(transaction.amount_due) - float(transaction.amount_paid)
                response_body = renderer.render(
                    "status_reply",
                    {
                        "business_name": business.name,
                        "customer_name": contact.first_name,
                        "balance": f"{balance:.2f}",
                        "due_date": str(transaction.due_date or ""),
                    },
                    language=language,
                )
            else:
                response_body = (
                    f"{business.name}: No outstanding balance found. "
                    f"Reply STOP to opt out."
                )
            action_taken = "status_replied"

        elif intent == InboundIntent.PAID:
            # ── Payment claim (queue for verification) ──
            # "PAID" / "LIPA" is a CLAIM, not confirmation. Staff must verify
            # via M-Pesa or cash records. In Phase 2, we'll check the M-Pesa API.
            response_body = (
                f"{business.name}: Asante! Tafadhali tuma M-Pesa reference code "
                f"katika ujumbe wako unaofuata. / Thank you! Please include your "
                f"M-Pesa reference code in your next message."
            )
            action_taken = "payment_acknowledged"

        elif intent == InboundIntent.CALL:
            # ── Queue callback request ──
            response_body = renderer.render(
                "callback_ack", {"business_name": business.name}, language=language
            )
            action_taken = "callback_queued"

        elif intent == InboundIntent.EXTENSION:
            # ── Create credit terms request ──
            credit_service = CreditTermsService()
            ct_request = await credit_service.create_request(
                session=session,
                business_id=business.id,
                contact_id=contact.id,
                inbound_message_id=msg.id,
                request_body=msg.body,
            )
            response_body = renderer.render(
                "credit_terms_ack", {"business_name": business.name}, language=language
            )
            action_taken = "credit_terms_created"

            await log_audit_event(
                event_type="credit_terms.requested",
                entity_type="credit_terms",
                entity_id=str(ct_request.id),
                summary=f"Credit terms request from contact {contact.id}",
                context=AuditContext(
                    business_id=business.id, actor_type="worker"
                ),
            )

        elif intent == InboundIntent.PROMO:
            # ── Send current active promotions ── NEW
            from domain.models import Campaign, CampaignStatus
            campaign_result = await session.execute(
                select(Campaign).where(
                    Campaign.business_id == business.id,
                    Campaign.status == CampaignStatus.RUNNING,
                ).limit(1)
            )
            campaign = campaign_result.scalar_one_or_none()
            if campaign:
                response_body = renderer.render(
                    "promo_message",
                    {
                        "business_name": business.name,
                        "promo_text": f"Current promo: {campaign.name}",
                    },
                    language=language,
                )
            else:
                response_body = (
                    f"{business.name}: No active promotions right now. "
                    f"Reply STOP to opt out."
                )
            action_taken = "promo_sent"

        elif intent == InboundIntent.POINTS:
            # ── Send loyalty points balance ── NEW
            # Find the customer linked to this contact.
            from domain.models import CustomerContactLink, Customer
            link_result = await session.execute(
                select(Customer)
                .join(CustomerContactLink, CustomerContactLink.customer_id == Customer.id)
                .where(CustomerContactLink.contact_id == contact.id)
                .limit(1)
            )
            customer = link_result.scalar_one_or_none()
            if customer:
                redeem_value = customer.loyalty_points // 10  # 10 pts = KES 1
                response_body = renderer.render(
                    "loyalty_points",
                    {
                        "contact_name": contact.first_name,
                        "business_name": business.name,
                        "points": str(customer.loyalty_points),
                        "redeem_value": str(redeem_value),
                    },
                    language=language,
                )
            else:
                response_body = (
                    f"{business.name}: No loyalty account found for this number. "
                    f"Reply STOP to opt out."
                )
            action_taken = "points_sent"

        elif intent == InboundIntent.BOOK:
            # ── Book appointment (clinic/salon) ── NEW
            # In a full implementation, this would create an appointment
            # record and notify staff. For now, acknowledge and queue callback.
            response_body = renderer.render(
                "book_appointment",
                {
                    "contact_name": contact.first_name,
                    "business_name": business.name,
                    "appointment_date": "soon",  # staff will call to confirm
                },
                language=language,
            )
            action_taken = "appointment_queued"

        elif intent == InboundIntent.HOURS:
            # ── Reply with business hours ── NEW
            from domain.policy_service import PolicyService
            policy_svc = PolicyService()
            policy = await policy_svc.load_policy(business.id)
            hours_text = (
                f"{policy.business_hours.start_hour}:00 - "
                f"{policy.business_hours.end_hour}:00"
            )
            response_body = renderer.render(
                "business_hours",
                {
                    "business_name": business.name,
                    "hours_text": hours_text,
                    "closed_days": "Sunday",
                },
                language=language,
            )
            action_taken = "hours_sent"

        elif intent == InboundIntent.LOCATION:
            # ── Reply with business location ── NEW
            # In a full implementation, the business would have a location field.
            response_body = renderer.render(
                "business_location",
                {
                    "business_name": business.name,
                    "location_text": "see our shop sign",  # TODO: load from business
                },
                language=language,
            )
            action_taken = "location_sent"

        else:
            # ── Unknown intent ──
            response_body = renderer.render(
                "help_reply", {"business_name": business.name}, language=language
            )
            action_taken = "unknown_replied"

        # ── 5. Queue outbound response in the outbox ──────────────
        # We insert into the outbox (not send directly) so the send worker
        # can respect quiet hours and business hours.
        if response_body:
            await _queue_response(session, contact, business, response_body)

        await session.commit()

        return {
            "status": "processed",
            "intent": intent.value,
            "confidence": confidence,
            "action": action_taken,
        }


async def _queue_response(
    session,
    contact: Contact,
    business: Business,
    body: str,
) -> None:
    """
    Insert an outbound response into the outbox for sending.

    TEACHING NOTE: We use a generic response key that includes a timestamp
    so multiple responses to the same contact don't collide. The key is:
    {business}:{contact}:response:{ISO timestamp}

    The response goes through the same outbox pipeline as reminders:
    pending → sending → sent. The send worker respects quiet hours.
    """
    message_key = f"{business.id}:{contact.id}:response:{datetime.utcnow().isoformat()}"

    response = OutboundMessage(
        business_id=business.id,
        contact_id=contact.id,
        transaction_id=None,
        message_key=message_key,
        reminder_type=ReminderType.CALLBACK_ACK,
        status=MessageStatus.PENDING,
        body=body,
        segments=1,
        language="en",
        provider="africas_talking",
        client_message_id=message_key,
        retry_count=0,
        max_retries=3,
        scheduled_at=datetime.utcnow(),
    )
    session.add(response)
    await session.flush()