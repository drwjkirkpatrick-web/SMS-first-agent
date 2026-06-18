"""
adapters/mock_adapter.py — Mock SMS Adapter for Testing
═══════════════════════════════════════════════════

Simulates SMS sends without hitting real APIs.
Useful for:
  - Unit tests (fast, no network)
  - Local development (no Africa's Talking or Twilio account needed)
  - CI/CD pipelines (no secrets needed)

Configure via environment: SMS_PROVIDER=mock

This adapter is INHERITED EXACTLY from the original tuition agent —
the mock behavior is provider-agnostic and needs no changes for the
Kenyan small business context.

Teaching notes:
  - The MockAdapter stores sent messages in a list so tests can
    assert that the right messages were "sent."
  - `success_rate` lets you simulate failures for testing retry logic.
  - `simulate_delays` adds artificial latency to test timeout handling.
  - Webhook signature validation always returns True (no crypto in tests).
═══════════════════════════════════════════════════
"""

import random
from datetime import datetime
from typing import Optional

from adapters.sms_adapter import (
    DeliveryQueryResult,
    ErrorCategory,
    SendResult,
    SendStatus,
    SMSAdapter,
)


class MockAdapter(SMSAdapter):
    """
    Mock SMS adapter for testing.

    Configurable behavior:
      - success_rate: probability of successful send (0.0–1.0)
      - simulate_delays: add asyncio.sleep to simulate network latency
    """

    def __init__(self, success_rate: float = 1.0, simulate_delays: bool = False) -> None:
        self.success_rate = success_rate
        self.simulate_delays = simulate_delays
        self._sent_messages: list[dict] = []

    async def send(
        self,
        to: str,
        body: str,
        client_message_id: Optional[str] = None,
    ) -> SendResult:
        if self.simulate_delays:
            import asyncio
            await asyncio.sleep(0.1)

        self._sent_messages.append({
            "to": to,
            "body": body,
            "client_message_id": client_message_id,
            "timestamp": datetime.utcnow().isoformat(),
        })

        if random.random() < self.success_rate:
            return SendResult(
                status=SendStatus.ACCEPTED,
                provider_message_id=f"MOCK_{random.randint(1000, 9999)}",
                client_message_id=client_message_id,
                segments=1,
            )
        else:
            return SendResult(
                status=SendStatus.REJECTED,
                client_message_id=client_message_id,
                error_message="Mock failure: simulated error",
                error_category=ErrorCategory.NON_RETRYABLE,
            )

    async def query_delivery(
        self,
        client_message_id: str,
    ) -> DeliveryQueryResult:
        # Check if we "sent" this message
        for msg in self._sent_messages:
            if msg.get("client_message_id") == client_message_id:
                return DeliveryQueryResult(
                    status="delivered",
                    provider_message_id=f"MOCK_{random.randint(1000, 9999)}",
                )
        return DeliveryQueryResult(status="not_found")

    async def validate_webhook_signature(
        self,
        body: bytes,
        signature: str,
        url: str,
    ) -> bool:
        return True  # always accept in mock mode

    def get_sent_messages(self) -> list[dict]:
        """Return all messages sent through this adapter (for test assertions)."""
        return list(self._sent_messages)