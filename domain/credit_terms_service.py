"""
domain/credit_terms_service.py — Credit terms extension request management
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Manages the lifecycle of credit terms extension requests (was
hardship_service.py in the tuition agent). A customer asks for more
time to pay off a credit or layaway transaction; the business owner /
staff reviews and responds manually within the SLA window.

Key operations:
  - create_request()   — from inbound SMS (EXTENSION keyword)
  - approve()           — approve the extension (staff action)
  - deny()              — deny the extension (staff action)
  - resolve()           — mark as resolved after customer is informed
  - find_overdue_requests() — SLA tracking (alert staff if open too long)

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - "HardshipRequest" → "CreditTermsRequest"
  - "HardshipStatus" → "CreditTermsStatus"
  - "HardshipService" → "CreditTermsService"
  - "School" → "Business", "Guardian" → "Contact", "Invoice" → "Transaction"
  - Same workflow: requested → under_review → approved/denied → resolved
  - Same SLA: 24 hours (configurable via SLA_HOURS class attribute)

INHERITED LOGIC
---------------
  - State machine: only valid transitions allowed (see VALID_TRANSITIONS).
  - SLA tracking: `sla_deadline` = created_at + SLA_HOURS.
  - Staff-facing: requests are NOT automated — a human must review.

TEACHING NOTES
--------------
  - Credit terms requests are staff-facing tickets, not automated.
  - The business owner or staff must manually review and respond.
  - SLA tracking lets us alert staff if a request has been open too long.
  - The `assigned_to` field supports routing requests to specific staff.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - `workers/inbound.py` calls `create_request()` when a customer texts
    "EXTENSION".
  - `domain/templates.py` provides the "credit_terms_ack" template for
    the acknowledgement SMS.
  - `workers/reminders.py` (or a dedicated SLA worker) calls
    `find_overdue_requests()` to alert staff.
  - `infra/audit_logger.py` logs the request creation and resolution.
═══════════════════════════════════════════════════════════════════════
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import (
    Contact,
    CreditTermsRequest,
    CreditTermsStatus,
    InboundMessage,
    Transaction,
)


class CreditTermsService:
    """
    Manages credit terms extension request lifecycle.

    TEACHING NOTE: The service is stateless. The SLA_HOURS class
    attribute is the only configuration — override it per-instance if
    a business needs a different SLA.
    """

    SLA_HOURS: int = 24  # default response time

    async def create_request(
        self,
        session: AsyncSession,
        business_id: int,
        contact_id: int,
        inbound_message_id: Optional[int] = None,
        transaction_id: Optional[int] = None,
        request_body: Optional[str] = None,
    ) -> CreditTermsRequest:
        """
        Create a new credit terms request from an inbound SMS.
        Sets SLA deadline and queues for staff review.
        """
        now = datetime.utcnow()
        sla_deadline = now + timedelta(hours=self.SLA_HOURS)

        request = CreditTermsRequest(
            business_id=business_id,
            contact_id=contact_id,
            transaction_id=transaction_id,
            inbound_message_id=inbound_message_id,
            status=CreditTermsStatus.REQUESTED,
            request_body=request_body,
            sla_deadline=sla_deadline,
            created_at=now,
            updated_at=now,
        )
        session.add(request)
        await session.flush()
        return request

    async def approve(
        self,
        session: AsyncSession,
        request: CreditTermsRequest,
        staff_notes: Optional[str] = None,
        assigned_to: Optional[str] = None,
    ) -> CreditTermsRequest:
        """
        Approve a credit terms request.

        TEACHING NOTE: Approving doesn't automatically change the
        transaction's due_date — that's a separate business decision
        the staff makes after approval (e.g., extend by 7 days, 14 days).
        This method only updates the request status.
        """
        return await self._update_status(
            session, request, CreditTermsStatus.APPROVED, staff_notes, assigned_to
        )

    async def deny(
        self,
        session: AsyncSession,
        request: CreditTermsRequest,
        staff_notes: Optional[str] = None,
        assigned_to: Optional[str] = None,
    ) -> CreditTermsRequest:
        """Deny a credit terms request."""
        return await self._update_status(
            session, request, CreditTermsStatus.DENIED, staff_notes, assigned_to
        )

    async def resolve(
        self,
        session: AsyncSession,
        request: CreditTermsRequest,
        staff_notes: Optional[str] = None,
    ) -> CreditTermsRequest:
        """
        Mark a request as resolved (after the customer has been informed
        of the decision and any new terms have been applied).
        """
        return await self._update_status(
            session, request, CreditTermsStatus.RESOLVED, staff_notes
        )

    async def _update_status(
        self,
        session: AsyncSession,
        request: CreditTermsRequest,
        new_status: CreditTermsStatus,
        staff_notes: Optional[str] = None,
        assigned_to: Optional[str] = None,
    ) -> CreditTermsRequest:
        """
        Internal: update request status with validation.

        INHERITED state machine:
          requested   → under_review, approved, denied
          under_review → approved, denied
          approved    → resolved
          denied      → resolved
          resolved    → (terminal)
        """
        valid = {
            CreditTermsStatus.REQUESTED: {
                CreditTermsStatus.UNDER_REVIEW,
                CreditTermsStatus.APPROVED,
                CreditTermsStatus.DENIED,
            },
            CreditTermsStatus.UNDER_REVIEW: {
                CreditTermsStatus.APPROVED,
                CreditTermsStatus.DENIED,
            },
            CreditTermsStatus.APPROVED: {CreditTermsStatus.RESOLVED},
            CreditTermsStatus.DENIED: {CreditTermsStatus.RESOLVED},
            CreditTermsStatus.RESOLVED: set(),
        }
        if new_status not in valid.get(request.status, set()):
            raise ValueError(
                f"Invalid transition from {request.status.value} to {new_status.value}"
            )

        request.status = new_status
        if staff_notes:
            request.staff_notes = staff_notes
        if assigned_to:
            request.assigned_to = assigned_to
        if new_status == CreditTermsStatus.RESOLVED:
            request.resolved_at = datetime.utcnow()
        request.updated_at = datetime.utcnow()
        await session.flush()
        return request

    async def find_overdue_requests(
        self,
        session: AsyncSession,
        business_id: int,
    ) -> list[CreditTermsRequest]:
        """
        Find credit terms requests past their SLA deadline.
        Called by a periodic worker to alert staff.
        """
        now = datetime.utcnow()
        result = await session.execute(
            select(CreditTermsRequest).where(
                CreditTermsRequest.business_id == business_id,
                CreditTermsRequest.status.in_([
                    CreditTermsStatus.REQUESTED,
                    CreditTermsStatus.UNDER_REVIEW,
                ]),
                CreditTermsRequest.sla_deadline < now,
            )
        )
        return list(result.scalars().all())