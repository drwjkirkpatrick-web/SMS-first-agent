"""
infra/audit_logger.py — Immutable, append-only audit event logging
═══════════════════════════════════════════════════

Every significant action in the system is logged to the audit_events
table. This is required for Kenya Data Protection Act (2019) compliance:

  - Article 72: Data controllers must maintain records of processing
  - Article 73: Records must be available for inspection by the ODPC
  - Article 84: Breach notification requires audit trail

The audit log is WRITE-ONLY in production:
  - INSERT only, never UPDATE or DELETE
  - `created_at` is server-set (not client-set) for tamper evidence
  - Summary field is safe for logs (PII masked)
  - Details field is JSON for deep investigation (may contain PII)

Teaching notes:
  - We use a dataclass for `AuditContext` to keep the calling code clean.
    Instead of passing 5 keyword args, you pass one context object.
  - `log_audit_event` is async because it writes to the DB.
  - If the audit log write fails, we DON'T suppress the original action.
    The audit log is a record, not a gate. (We log the failure separately.)
  - `summary` should be human-readable: "Guardian 42 opted out via SMS"
  - `details` should be machine-readable: JSON with before/after values
═══════════════════════════════════════════════════
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import AuditEvent, AuditEventType
from infra.database import async_session_factory


@dataclass
class AuditContext:
    """
    Carries audit metadata so calling code stays clean.
    Pass this instead of 4 separate kwargs.
    """
    school_id: Optional[int] = None   # renamed to business_id in practice
    actor_type: str = "system"          # "system", "worker", "user", "api"
    actor_id: Optional[str] = None      # staff name, worker hostname, etc.
    source: Optional[str] = None        # IP address or worker ID


async def log_audit_event(
    *,
    event_type: str,
    entity_type: str,
    entity_id: Optional[str],
    summary: str,
    details: Optional[str] = None,
    context: Optional[AuditContext] = None,
    session: Optional[AsyncSession] = None,
) -> None:
    """
    Write an immutable audit event.

    Args:
        event_type: one of AuditEventType values (e.g., "message.send_attempt")
        entity_type: "message", "customer", "transaction", etc.
        entity_id: the primary key of the affected entity
        summary: human-readable, PII-safe description
        details: JSON string with full details (may contain PII)
        context: audit context (actor, source, school_id)
        session: optional AsyncSession. If provided, the audit event
            is inserted into this session's transaction (S7: transactional
            audit logging). If not provided, a standalone session is
            created so the audit event commits independently of any
            caller transaction.

    S7 Teaching note: When a session is provided, the audit event
    participates in the caller's transaction. If the caller's transaction
    rolls back, the audit event is also rolled back — preserving causal
    consistency. When no session is provided, we use a fresh session so
    the audit event persists even if the caller's transaction fails
    (tamper evidence for security incidents).
    """
    ctx = context or AuditContext()

    async def _insert(s: AsyncSession) -> None:
        event = AuditEvent(
            school_id=ctx.school_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            details=details,
            actor_type=ctx.actor_type,
            actor_id=ctx.actor_id,
            source=ctx.source,
        )
        s.add(event)

    if session is not None:
        # S7: Insert into the caller's transaction (no separate commit).
        # The caller controls when to commit or rollback.
        await _insert(session)
    else:
        # Standalone: use a fresh session and commit independently.
        async with async_session_factory() as standalone_session:
            await _insert(standalone_session)
            await standalone_session.commit()