"""
adapters/sms_adapter.py — SMS Adapter Interface
═══════════════════════════════════════════════════

Abstract base class for SMS providers. This lets us:
  - Support multiple providers (Africa's Talking, Twilio, Mock) with one codebase
  - Mock the adapter in tests (no real SMS sends during testing)
  - Swap providers without changing worker code

This interface is INHERITED EXACTLY from the original tuition agent —
the abstract SMSAdapter, SendStatus, ErrorCategory, SendResult, and
DeliveryQueryResult are provider-agnostic and need zero changes for
the Kenyan small business context.

Teaching notes:
  - The adapter normalizes all provider responses into our own dataclass.
    This means workers don't need to know if it's Africa's Talking,
    Twilio, or a mock.
  - `client_message_id` is CRITICAL for idempotency. We pass this to the
    provider; if we retry with the same ID, the provider deduplicates
    on their side. This is a core part of the 12-layer anti-duplicate
    defense.
  - We distinguish RETRYABLE errors (network timeout, rate limit) from
    NON-RETRYABLE errors (invalid phone number, account suspended).
    Retryable → mark UNKNOWN_DELIVERY; Non-retryable → mark FAILED.
  - For Kenya: the same interface handles both Africa's Talking (primary)
    and Twilio (fallback). Workers call `adapter.send()` — they don't
    know or care which provider is active.

Kenya-specific considerations:
  - `price` field should be populated in KES for Africa's Talking,
    though the field is unit-agnostic at this interface level.
  - Phone numbers come in as E.164 (+2547XXXXXXXXX). The adapter
    is responsible for any format normalization the provider needs.
═══════════════════════════════════════════════════
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class SendStatus(str, Enum):
    """Normalized send result status."""
    ACCEPTED = "accepted"      # provider accepted, assigned message ID
    REJECTED = "rejected"      # permanent failure (invalid number, etc.)
    TIMEOUT = "timeout"          # network/API timeout — ambiguous
    RATE_LIMITED = "rate_limited"  # too fast — retry after delay
    UNKNOWN = "unknown"          # unexpected response


class ErrorCategory(str, Enum):
    """Classifies errors for retry decisions."""
    RETRYABLE = "retryable"      # network, timeout, rate limit, provider outage
    NON_RETRYABLE = "non_retryable"  # invalid number, blocked, account issue
    AMBIGUOUS = "ambiguous"      # timeout after submit — need reconciliation


@dataclass(frozen=True)
class SendResult:
    """
    Normalized result of an SMS send attempt.

    All provider-specific responses are mapped to this standard format.
    This is what the outbox worker receives and stores in the database.
    """
    status: SendStatus
    provider_message_id: Optional[str] = None   # Twilio Message SID, AT message ID, etc.
    client_message_id: Optional[str] = None       # our idempotent key
    segments: int = 1
    price: Optional[float] = None                 # cost (KES for Africa's Talking, USD for Twilio)
    error_code: Optional[str] = None              # provider error code
    error_message: Optional[str] = None           # human-readable error
    error_category: ErrorCategory = ErrorCategory.RETRYABLE
    raw_response: Optional[dict] = None           # full provider response for debugging


@dataclass(frozen=True)
class DeliveryQueryResult:
    """Result of querying a provider for an ambiguous delivery."""
    status: str  # "sent", "delivered", "failed", "not_found", "unknown"
    provider_message_id: Optional[str] = None
    delivered_at: Optional[datetime] = None
    error_code: Optional[str] = None


class SMSAdapter(ABC):
    """
    Abstract base class for SMS provider adapters.

    Every SMS provider — Africa's Talking, Twilio, Mock — must implement
    these three methods. The outbox worker, reconciliation loop, and webhook
    handlers all depend on this interface, never on a concrete class.
    """

    @abstractmethod
    async def send(
        self,
        to: str,
        body: str,
        client_message_id: Optional[str] = None,
    ) -> SendResult:
        """
        Send an SMS message.

        Args:
            to: E.164 phone number (+2547XXXXXXXXX for Kenya)
            body: message content (max 160 * max_segments chars)
            client_message_id: idempotent reference (must be unique per message)

        Returns:
            SendResult with normalized status
        """
        ...

    @abstractmethod
    async def query_delivery(
        self,
        client_message_id: str,
    ) -> DeliveryQueryResult:
        """
        Query the provider for the status of a previously submitted message.
        Used by the reconciliation loop for UNKNOWN_DELIVERY states.

        Args:
            client_message_id: the idempotent key we sent originally

        Returns:
            DeliveryQueryResult with current provider status
        """
        ...

    @abstractmethod
    async def validate_webhook_signature(
        self,
        body: bytes,
        signature: str,
        url: str,
    ) -> bool:
        """
        Validate that a webhook callback actually came from the provider.
        Protects against spoofed delivery receipts and inbound message
        injections.

        Args:
            body: raw request body bytes (must be the exact bytes received)
            signature: signature header value from the provider
            url: the full URL of the webhook endpoint

        Returns:
            True if the signature is valid, False otherwise
        """
        ...