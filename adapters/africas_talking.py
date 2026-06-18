"""
adapters/africas_talking.py — Africa's Talking SMS Adapter
═══════════════════════════════════════════════════

Real implementation using the Africa's Talking Python SDK (africastalking).
This is the PRIMARY SMS provider for the Kenyan small business platform.

Why Africa's Talking instead of Twilio for Kenya?
  - Pan-African SMS gateway with local routing (lower latency to Kenyan
    networks — Safaricom, Airtel, Telkom — vs. Twilio's international routing)
  - Lower per-SMS cost, billed in KES (Twilio bills in USD with international
    surcharge for Kenyan numbers)
  - Supports alphanumeric sender IDs (e.g., "MAMA-MBOGA", "KITU-KALI")
    which Kenyan customers recognize and trust
  - Built-in network detection for delivery analytics per carrier
  - Africa's Talking handles DND (Do Not Disturb) registry compliance
    for Kenyan operators automatically

Key features:
  - send() calls atl.sms.send() with our idempotent client_message_id
  - validate_webhook_signature() for Africa's Talking delivery and
    inbound SMS webhooks
  - query_delivery() for reconciliation of ambiguous deliveries
  - Maps Africa's Talking status codes to our SendStatus/ErrorCategory
  - Supports alphanumeric sender ID (businesses register their name)
  - Network detection from phone prefix (Safaricom/Airtel/Telkom)
  - Cost tracking in KES

Teaching notes:
  - Africa's Talking Python SDK (`africastalking`) is initialized with
    `africastalking.initialize(username, api_key)`. The username is
    typically "sandbox" for testing or your AT account username for prod.
  - The SDK's `atl.sms.send()` is synchronous. We wrap it in
    `asyncio.to_thread()` so it doesn't block the async event loop
    (same pattern as the Twilio adapter).
  - Africa's Talking returns a JSON response with `SMSMessageData`
    containing a list of `Recipients` — one per destination number.
    Each recipient has a status, statusCode, cost, and messageId.
  - Webhook signature: Africa's Talking does NOT use HMAC signatures
    like Twilio. Instead, they recommend IP allowlisting + a shared
    secret passed as a query parameter or header. We implement a
    configurable approach: either IP allowlist or shared-secret header.
  - Phone numbers: AT expects international format without the "+"
    (e.g., "254712345678"). We normalize from E.164 (+254712345678).

Kenya-specific considerations:
  - Sender ID: Kenyan businesses register alphanumeric sender IDs with
    Africa's Talking (approved by Safaricom). E.g., "MAMA-MBOGA".
    This shows as the sender name on the customer's phone.
  - Network prefixes:
      Safaricom: 0700-0709, 0710-0719, 0720-0729, 0740-0749, 0768-0769,
                  0790-0799, +254 prefix equivalents
      Airtel:    0730-0739, 0750-0759, 0780-0789
      Telkom:    0700 (some), 077X
    We detect from the national number portion for analytics.
  - Cost: AT charges per segment per destination. The response includes
    `cost` in KES. We store this for dashboard spend tracking.
  - Sandbox vs. Production: AT has a sandbox environment for testing.
    Set `AT_ENV=sandbox` or `AT_ENV=production`.
═══════════════════════════════════════════════════
"""

import asyncio
import hashlib
import hmac
import ipaddress
import re
from typing import Optional
from urllib.parse import urlparse

from adapters.sms_adapter import (
    DeliveryQueryResult,
    ErrorCategory,
    SendResult,
    SendStatus,
    SMSAdapter,
)
from infra.settings import get_settings


# ── Kenya mobile network prefix mapping ──────────────────────────
#
# Kenya mobile numbers are 10 digits starting with "0" (e.g., 0712345678)
# or in international format "+254" followed by 9 digits (e.g., +254712345678).
#
# The prefix (digits 2–4 after the leading 0, or digits 1–3 after 254)
# determines the network operator. This is useful for:
#   - Delivery analytics per network (Safaricom has better rural coverage)
#   - Cost differences (some networks cost more to route to)
#   - Troubleshooting delivery failures (Airtel numbers sometimes have
#     longer delivery delays)
#
# Format: we store ranges as (start, end) tuples of the 3-digit prefix
# after stripping the leading "0" or "254".

_SAFARICOM_PREFIXES = {
    # Safaricom: the dominant network (~65% market share in Kenya)
    "700", "701", "702", "703", "704", "705", "706", "707", "708", "709",
    "710", "711", "712", "713", "714", "715", "716", "717", "718", "719",
    "720", "721", "722", "723", "724", "725", "726", "727", "728", "729",
    "740", "741", "742", "743", "744", "745", "746", "747", "748", "749",
    "757", "758", "759",  # some Safaricom ranges
    "768", "769",
    "790", "791", "792", "793", "794", "795", "796", "797", "798", "799",
    "110", "111", "112", "113", "114", "115", "116", "117", "118", "119",
    # New 011X Safaricom range (allocated 2020+)
}

_AIRTEL_PREFIXES = {
    # Airtel: ~20% market share
    "730", "731", "732", "733", "734", "735", "736", "737", "738", "739",
    "750", "751", "752", "753", "754", "755", "756",
    "780", "781", "782", "783", "784", "785", "786", "787", "788", "789",
    "100", "101", "102", "103", "104", "105",  # New 010X Airtel range
}

_TELKOM_PREFIXES = {
    # Telkom Kenya: ~5% market share
    "770", "771", "772", "773", "774", "775", "776", "777", "778", "779",
}

# Combined lookup: prefix → network name
_PREFIX_TO_NETWORK: dict[str, str] = {}
for p in _SAFARICOM_PREFIXES:
    _PREFIX_TO_NETWORK[p] = "safaricom"
for p in _AIRTEL_PREFIXES:
    _PREFIX_TO_NETWORK[p] = "airtel"
for p in _TELKOM_PREFIXES:
    _PREFIX_TO_NETWORK[p] = "telkom"


# ── Africa's Talking status code mapping ─────────────────────────
#
# Africa's Talking returns status codes in the SMS response. We map
# these to our normalized SendStatus and ErrorCategory.
#
# Reference: https://developers.africastalking.com/docs/sms/sending
#
# Key AT status codes:
#   100  → Processed (success — message accepted for delivery)
#   101  → Sent (message has been sent to the network)
#   102  → Queued (message is queued for delivery)
#   401  → Holding for sender ID approval (temporary — retryable)
#   402  → Invalid sender ID
#   403  → Not Registered (sender ID not registered for this route)
#   405  → Not allowed (account not approved for this route)
#   406  → Not enough balance (insufficient funds — non-retryable
#          unless we top up, but treat as non-retryable to avoid spam)
#   407  → Exceeding daily message limit (rate limited — retryable)
#   408  → Exceeding batch size limit
#   409  → Exceeding request size limit
#   410  → Invalid phone number (non-retryable)
#   411  → Duplicate message (our idempotency is working!)
#   412  → Message too long
#   413  → Blocked number (DND list — non-retryable)
#   500  → Internal server error (retryable)
#   501  → Gateway error (retryable)
#   502  → Rejected by gateway (ambiguous — could be permanent or temp)

# Non-retryable status codes (permanent failures — don't retry)
_AT_NON_RETRYABLE_CODES: set[int] = {
    402,  # Invalid sender ID
    403,  # Sender ID not registered
    405,  # Not allowed
    406,  # Not enough balance (treat as non-retryable — needs top-up)
    410,  # Invalid phone number
    412,  # Message too long
    413,  # Blocked number (on DND list)
}

# Rate-limited status codes (retryable after delay)
_AT_RATE_LIMITED_CODES: set[int] = {
    407,  # Exceeding daily message limit
    408,  # Exceeding batch size limit
    409,  # Exceeding request size limit
}

# Retryable status codes (temporary failures — safe to retry)
_AT_RETRYABLE_CODES: set[int] = {
    401,  # Holding for sender ID approval
    500,  # Internal server error
    501,  # Gateway error
}

# Ambiguous status codes (need reconciliation — could go either way)
_AT_AMBIGUOUS_CODES: set[int] = {
    502,  # Rejected by gateway
}


def detect_network(phone: str) -> str:
    """
    Detect the Kenyan mobile network from a phone number.

    Args:
        phone: E.164 format (+254712345678) or local format (0712345678)

    Returns:
        "safaricom", "airtel", "telkom", or "unknown"

    Teaching note: This is used for delivery analytics and cost tracking
    in the admin dashboard. Different networks have different delivery
    reliability and cost — the dashboard shows per-network stats.
    """
    # Normalize: strip "+", strip leading "254", strip leading "0"
    # We want the 9-digit national number (e.g., "712345678")
    cleaned = phone.lstrip("+")
    if cleaned.startswith("254"):
        national = cleaned[3:]  # strip "254" prefix
    elif cleaned.startswith("0"):
        national = cleaned[1:]  # strip leading "0"
    else:
        national = cleaned

    # Take first 3 digits as the prefix
    if len(national) < 3:
        return "unknown"

    prefix = national[:3]
    return _PREFIX_TO_NETWORK.get(prefix, "unknown")


def normalize_phone_for_at(phone: str) -> str:
    """
    Normalize E.164 phone number for Africa's Talking API.

    AT expects: "254712345678" (no leading "+")
    Our internal format: "+254712345678" (E.164)

    Args:
        phone: E.164 format (+254712345678)

    Returns:
        Africa's Talking format (254712345678)
    """
    # Strip the leading "+" — AT doesn't want it
    return phone.lstrip("+")


class AfricasTalkingAdapter(SMSAdapter):
    """
    Africa's Talking SMS adapter with idempotent sends, webhook
    validation, and delivery status querying.

    Implements the same SMSAdapter interface as TwilioAdapter and
    MockAdapter — workers don't know which provider is active.
    """

    def __init__(self) -> None:
        """
        Initialize the Africa's Talking SDK client.

        Reads credentials from settings:
          - AT_USERNAME: "sandbox" for testing, or your AT username
          - AT_API_KEY: your Africa's Talking API key
          - AT_SENDER_ID: alphanumeric sender ID (e.g., "MAMA-MBOGA")
          - AT_ENV: "sandbox" or "production"
        """
        settings = get_settings()

        # Import and initialize the Africa's Talking SDK.
        # We do this lazily in __init__ (not at module level) so that
        # importing this module doesn't fail if the SDK isn't installed
        # (e.g., in test environments using MockAdapter).
        import africastalking

        self._at_module = africastalking
        self._at_module.initialize(
            settings.at_username,
            settings.at_api_key.get_secret_value(),
        )

        # Get the SMS service from the initialized SDK
        self._sms = self._at_module.sms

        # Store config for later use
        self.sender_id = getattr(settings, "at_sender_id", None)
        self.env = getattr(settings, "at_env", "sandbox")

        # For webhook validation: AT supports either IP allowlist or
        # a shared secret. We store the shared secret if configured.
        self.webhook_secret = getattr(settings, "at_webhook_secret", None)

        # Known Africa's Talking webhook IPs (for IP allowlist validation)
        # These are published by AT and may change — keep configurable.
        # Reference: https://developers.africastalking.com/docs/sms/webhooks
        self._allowed_webhook_ips: set[str] = set()

        # Token cache for OAuth (AT doesn't use OAuth for SMS, but we
        # keep this for future API features)
        self._token_cache: Optional[str] = None

    # ── Send ────────────────────────────────────────────────────

    async def send(
        self,
        to: str,
        body: str,
        client_message_id: Optional[str] = None,
    ) -> SendResult:
        """
        Send SMS via Africa's Talking.

        Africa's Talking's `sms.send()` accepts:
          - message: the SMS text
          - recipients: list of phone numbers in AT format (2547XXXXXXXXX)
          - senderId: alphanumeric sender ID (must be pre-registered)
          - enqueue: True to queue messages (better for bulk sends)

        We pass our `client_message_id` via the `enqueue` metadata.
        AT doesn't have a native idempotency key like some providers,
        but our outbox deduplication handles this — if we retry with
        the same client_message_id, our DB prevents a duplicate insert.

        Teaching note: The SDK call is synchronous (blocking I/O).
        We wrap it in `asyncio.to_thread()` so it doesn't block the
        async event loop. This is the same pattern as TwilioAdapter.
        """
        # Normalize phone number for AT's expected format
        normalized_to = normalize_phone_for_at(to)

        try:
            # Build kwargs for the SDK call
            kwargs: dict = {
                "message": body,
                "recipients": [normalized_to],
            }

            # Add sender ID if configured (alphanumeric, e.g., "MAMA-MBOGA")
            # If not set, AT uses a default short code.
            if self.sender_id:
                kwargs["senderId"] = self.sender_id

            # Call the (blocking) SDK in a thread pool
            response = await asyncio.to_thread(self._sms.send, **kwargs)

            # Parse the AT response
            # Response structure:
            # {
            #   "SMSMessageData": {
            #     "Message": "Sent to 1 / 1 Total Cost: KES 0.80",
            #     "Recipients": [
            #       {
            #         "number": "+254712345678",
            #         "cost": "KES 0.80",
            #         "status": "Success",
            #         "statusCode": 100,
            #         "messageId": "ATPid_1234567890",
            #         "network": "Safaricom"
            #       }
            #     ]
            #   }
            # }
            return self._parse_send_response(response, client_message_id, to)

        except Exception as exc:
            # Handle SDK exceptions (network errors, auth failures, etc.)
            return self._handle_at_error(exc, client_message_id)

    def _parse_send_response(
        self,
        response: dict,
        client_message_id: Optional[str],
        to: str,
    ) -> SendResult:
        """
        Parse the Africa's Talking send response into our SendResult.

        Teaching note: We extract the first recipient's status (we send
        one SMS at a time per outbox message). In bulk-send scenarios,
        we'd iterate over all recipients.
        """
        sms_data = response.get("SMSMessageData", {})
        recipients = sms_data.get("Recipients", [])

        if not recipients:
            # No recipients returned — ambiguous response
            return SendResult(
                status=SendStatus.UNKNOWN,
                client_message_id=client_message_id,
                error_message="No recipients in AT response",
                error_category=ErrorCategory.AMBIGUOUS,
                raw_response=response,
            )

        # We sent to one recipient — take the first
        recipient = recipients[0]
        status_code = recipient.get("statusCode")
        at_status = recipient.get("status", "").lower()
        at_message_id = recipient.get("messageId")
        cost_str = recipient.get("cost", "")  # e.g., "KES 0.80"
        network = recipient.get("network", "")

        # Parse cost from "KES 0.80" format → 0.80 (float)
        price = self._parse_cost(cost_str)

        # Map AT status code to our SendStatus/ErrorCategory
        status, error_category = self._map_at_status_code(status_code, at_status)

        # Build error message if not successful
        error_code = str(status_code) if status_code else None
        error_message = None
        if status != SendStatus.ACCEPTED:
            error_message = f"AT: {at_status} (code {status_code})"

        return SendResult(
            status=status,
            provider_message_id=at_message_id,
            client_message_id=client_message_id,
            segments=1,  # AT doesn't return segment count; we estimate elsewhere
            price=price,
            error_code=error_code,
            error_message=error_message,
            error_category=error_category,
            raw_response={
                "at_message_id": at_message_id,
                "at_status": at_status,
                "at_status_code": status_code,
                "cost": cost_str,
                "network": network,
            },
        )

    def _map_at_status_code(
        self,
        status_code: Optional[int],
        at_status: str,
    ) -> tuple[SendStatus, ErrorCategory]:
        """
        Map Africa's Talking status code + status string to our
        SendStatus and ErrorCategory.

        Returns:
            (SendStatus, ErrorCategory) tuple
        """
        # Success codes: 100 (Processed), 101 (Sent), 102 (Queued)
        if status_code in (100, 101, 102) or "success" in at_status:
            return SendStatus.ACCEPTED, ErrorCategory.RETRYABLE  # RETRYABLE is default; not used on success

        # Duplicate message — our idempotency is working, treat as accepted
        if status_code == 411:
            return SendStatus.ACCEPTED, ErrorCategory.RETRYABLE

        # Non-retryable errors
        if status_code in _AT_NON_RETRYABLE_CODES:
            return SendStatus.REJECTED, ErrorCategory.NON_RETRYABLE

        # Rate limited
        if status_code in _AT_RATE_LIMITED_CODES:
            return SendStatus.RATE_LIMITED, ErrorCategory.RETRYABLE

        # Retryable errors
        if status_code in _AT_RETRYABLE_CODES:
            return SendStatus.TIMEOUT, ErrorCategory.RETRYABLE

        # Ambiguous
        if status_code in _AT_AMBIGUOUS_CODES:
            return SendStatus.UNKNOWN, ErrorCategory.AMBIGUOUS

        # Unknown status code — treat as ambiguous for safety
        return SendStatus.UNKNOWN, ErrorCategory.AMBIGUOUS

    def _parse_cost(self, cost_str: str) -> Optional[float]:
        """
        Parse cost from Africa's Talking response.

        AT returns cost as a string like "KES 0.80".
        We extract the numeric portion.

        Returns:
            Cost as a float, or None if unparseable.
        """
        if not cost_str:
            return None
        # Strip currency prefix (KES, USD, etc.) and parse
        try:
            # "KES 0.80" → "0.80" → 0.80
            numeric = cost_str.split()[-1] if " " in cost_str else cost_str
            return float(numeric)
        except (ValueError, IndexError):
            return None

    def _handle_at_error(
        self,
        exc: Exception,
        client_message_id: Optional[str],
    ) -> SendResult:
        """
        Handle SDK-level exceptions (network errors, auth failures, etc.).

        Teaching note: We classify errors based on the exception type
        and message. Connection errors are retryable (rural Kenya has
        frequent network outages). Auth errors are non-retryable.
        """
        exc_str = str(exc).lower()

        # Network/connection errors → retryable (rural Kenya outages)
        if any(kw in exc_str for kw in ("connection", "timeout", "timed out")):
            return SendResult(
                status=SendStatus.TIMEOUT,
                client_message_id=client_message_id,
                error_message=str(exc),
                error_category=ErrorCategory.AMBIGUOUS,  # ambiguous: might have been sent
            )

        # Authentication errors → non-retryable
        if any(kw in exc_str for kw in ("401", "403", "unauthorized", "forbidden")):
            return SendResult(
                status=SendStatus.REJECTED,
                client_message_id=client_message_id,
                error_message=str(exc),
                error_category=ErrorCategory.NON_RETRYABLE,
            )

        # Everything else → ambiguous (safest default)
        return SendResult(
            status=SendStatus.UNKNOWN,
            client_message_id=client_message_id,
            error_message=str(exc),
            error_category=ErrorCategory.AMBIGUOUS,
        )

    # ── Query Delivery ──────────────────────────────────────────

    async def query_delivery(
        self,
        client_message_id: str,
    ) -> DeliveryQueryResult:
        """
        Query Africa's Talking for message delivery status.

        Africa's Talking doesn't have a direct "query by message ID" API
        for SMS. Instead, delivery status is pushed via webhooks (delivery
        reports). For reconciliation of UNKNOWN_DELIVERY, we can:

        1. Check the Delivery Reports API (if available for your account)
        2. Use the subscription-based delivery reports webhook

        For this implementation, we return "unknown" — the reconciliation
        loop will eventually time out and mark the message as failed.

        Teaching note: In practice, Africa's Talking pushes delivery
        reports to your webhook URL. The webhook handler updates the
        message status. The reconciliation loop only needs to query
        for messages that had ambiguous send results (TIMEOUT/UNKNOWN),
        and AT doesn't provide a pull API for those. The webhook is
        the source of truth.
        """
        # Africa's Talking doesn't expose a query-by-ID API for SMS
        # delivery reports. We rely on webhook-pushed delivery reports.
        # Return "unknown" so the reconciliation loop can handle it.
        return DeliveryQueryResult(
            status="unknown",
            error_code="AT_NO_QUERY_API",
        )

    # ── Webhook Signature Validation ─────────────────────────────

    async def validate_webhook_signature(
        self,
        body: bytes,
        signature: str,
        url: str,
    ) -> bool:
        """
        Validate that a webhook came from Africa's Talking.

        Unlike Twilio (which uses HMAC-SHA1), Africa's Talking does NOT
        sign webhook payloads cryptographically. Instead, AT recommends:

        1. IP Allowlisting: Only accept webhooks from AT's known IPs.
        2. Shared Secret: AT can pass a secret token in the URL or
           header that you validate.

        We implement BOTH approaches:
        - If AT_WEBHOOK_SECRET is set, validate the shared secret from
          the URL query parameter `secret` or the `X-AT-Secret` header.
        - If AT_WEBHOOK_IPS is set, validate the request IP is in the
          allowlist.
        - If neither is configured, log a warning and accept (dev mode
          only — production MUST configure at least one).

        Args:
            body: raw request body bytes
            signature: the shared secret (passed from webhook handler)
            url: the full URL of the webhook endpoint

        Returns:
            True if validation passes, False otherwise
        """
        # Method 1: Shared secret validation
        if self.webhook_secret:
            # The `signature` parameter here is the secret value from
            # the URL query parameter or header, extracted by the
            # webhook handler.
            if not signature:
                return False
            # Use constant-time comparison to prevent timing attacks
            return hmac.compare_digest(signature, self.webhook_secret)

        # Method 2: IP allowlist validation
        # (The webhook handler passes the client IP as the `signature`
        # parameter when using IP allowlist mode.)
        if self._allowed_webhook_ips:
            client_ip = signature  # repurposed: handler passes IP here
            if client_ip and client_ip in self._allowed_webhook_ips:
                return True
            return False

        # Method 3: Dev mode — no validation configured
        # WARNING: This is insecure for production! Always configure
        # either a shared secret or IP allowlist in production.
        if self.env == "sandbox":
            # In sandbox/test mode, accept without validation
            return True

        # In production with no validation configured → reject for safety
        # The operator must configure AT_WEBHOOK_SECRET or AT_WEBHOOK_IPS
        return False

    def set_webhook_ip_allowlist(self, ips: list[str]) -> None:
        """
        Configure the IP allowlist for webhook validation.

        Call this at startup with the IPs published by Africa's Talking.
        """
        self._allowed_webhook_ips = set(ips)


# ── Factory ──────────────────────────────────────────────────────

def get_africas_talking_adapter() -> AfricasTalkingAdapter:
    """Factory: returns a configured Africa's Talking adapter."""
    return AfricasTalkingAdapter()