"""
adapters/mpesa_adapter.py — M-Pesa Integration via Safaricom Daraja API
═══════════════════════════════════════════════════

M-Pesa is Kenya's dominant mobile money platform (>70% of transactions).
This adapter integrates with Safaricom's Daraja API for:

  1. STK Push (Lipa na M-Pesa Online):
     - Business triggers a payment prompt on the customer's phone
     - Customer enters M-Pesa PIN to authorize
     - Safaricom sends a callback webhook with the result
     - We match the payment to a customer/transaction

  2. C2B (Customer-to-Business):
     - Customer manually sends money to the business Paybill/Till number
     - Safaricom sends validation + confirmation webhooks
     - We match and record the payment

  3. OAuth Token Management:
     - Daraja API uses OAuth 2.0 for authentication
     - Tokens expire every ~60 minutes; we cache and refresh them

Key methods:
  - get_oauth_token(): Get/refresh the Safaricom OAuth access token
  - trigger_stk_push(): Send an STK Push prompt to a customer's phone
  - validate_mpesa_webhook(): Validate M-Pesa callback authenticity
  - parse_c2b_confirmation(): Parse a C2B confirmation webhook payload
  - parse_stk_callback(): Parse an STK Push result callback

Teaching notes:
  - The Daraja API has sandbox and production environments. Sandbox
    uses test credentials and fake phone numbers (e.g., 254708374149).
    Production requires Safaricom organization approval.
  - OAuth tokens are obtained via HTTP Basic Auth using your Consumer
    Key + Consumer Secret. The token is passed as a Bearer token in
    subsequent API calls.
  - STK Push requires a "Password" parameter = Base64(Shortcode + Passkey +
    Timestamp). The passkey is obtained from Safaricom when you register
    for Lipa na M-Pesa Online.
  - Webhook validation: Safaricom doesn't sign webhooks cryptographically
    (like Twilio). Instead, they use:
      a. HTTPS-only endpoints (TLS provides transport security)
      b. IP allowlisting (Safaricom publishes their API gateway IPs)
      c. The webhook payload contains a `BusinessShortCode` that must
         match your configured shortcode.
  - All amounts are in KES (Kenya Shillings), as floats or integers.

Kenya-specific considerations:
  - M-Pesa is THE payment rail in Kenya. Integration is not optional
    for a Kenyan small business platform.
  - Paybill numbers are used by formal businesses (registered Ltds).
    Till numbers are used by informal businesses (sole proprietors,
    mama mboga, etc.). The adapter supports both.
  - The Daraja API is rate-limited. STK Push has per-shortcode limits.
  - Network outages in rural Kenya can cause STK Push timeouts. The
    webhook callback handles this gracefully — if the customer doesn't
    respond, we get a "failed" callback with a specific error code.
  - Sandbox URL: https://sandbox.safaricom.co.ke
    Production URL: https://api.safaricom.co.ke
═══════════════════════════════════════════════════
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Any, Optional

import httpx

from infra.settings import get_settings


# ── Environment URLs ────────────────────────────────────────────

# Safaricom Daraja API base URLs
# Sandbox: used for development and testing with fake credentials
# Production: used for live transactions with real money
_DARAJA_URLS = {
    "sandbox": {
        "base": "https://sandbox.safaricom.co.ke",
        "oauth": "https://sandbox.safaricom.co.ke/oauth/v1/generate",
        "stk_push": "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
        "stk_query": "https://sandbox.safaricom.co.ke/mpesa/stkpushquery/v1/query",
        "c2b_register": "https://sandbox.safaricom.co.ke/mpesa/c2b/v1/registerurl",
    },
    "production": {
        "base": "https://api.safaricom.co.ke",
        "oauth": "https://api.safaricom.co.ke/oauth/v1/generate",
        "stk_push": "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
        "stk_query": "https://api.safaricom.co.ke/mpesa/stkpushquery/v1/query",
        "c2b_register": "https://api.safaricom.co.ke/mpesa/c2b/v1/registerurl",
    },
}


# ── M-Pesa result codes ─────────────────────────────────────────
#
# M-Pesa callbacks include a `ResultCode` field:
#   0    → Success (payment completed)
#   1032 → Request cancelled by user
#   1037 → DS timeout (cannot reach subscriber — phone off, no signal)
#   1001 → Insufficient funds
#   1019 → Transaction failed (unknown reason)
#   1025 → System busy, try again later
#   1026 → Invalid account number
#   1027 → System error
#   1029 → Unresolved primary party (wrong paybill)
#   1030 → Invalid amount
#   9999 → Unknown error

# Success codes (payment was completed)
MPESA_SUCCESS_CODE = 0

# Retryable result codes (temporary failure — customer can try again)
MPESA_RETRYABLE_CODES: set[int] = {
    1025,  # System busy
    1037,  # DS timeout (phone unreachable — could be temporary)
}

# Non-retryable result codes (permanent failure — don't retry automatically)
MPESA_NON_RETRYABLE_CODES: set[int] = {
    1032,  # Cancelled by user
    1001,  # Insufficient funds
    1026,  # Invalid account number
    1029,  # Unresolved primary party
    1030,  # Invalid amount
    1019,  # Transaction failed
}


class MpesaAdapter:
    """
    M-Pesa integration adapter using Safaricom Daraja API.

    This is NOT an SMS adapter — it's a payment adapter. It doesn't
    implement the SMSAdapter interface. Instead, it provides methods
    for triggering M-Pesa payments and parsing M-Pesa webhooks.

    The workflow:
    1. Business triggers STK Push → customer gets a PIN prompt
    2. Customer enters PIN → Safaricom processes payment
    3. Safaricom sends STK callback webhook → we parse and match
    4. For C2B: customer sends money → Safaricom sends confirmation
       webhook → we parse and match

    All amounts are in KES (Kenya Shillings).
    """

    def __init__(self) -> None:
        """
        Initialize the M-Pesa adapter with Daraja API credentials.

        Reads from settings:
          - MPESA_ENV: "sandbox" or "production"
          - MPESA_CONSUMER_KEY: Daraja API consumer key
          - MPESA_CONSUMER_SECRET: Daraja API consumer secret
          - MPESA_SHORTCODE: Business Paybill or Till number
          - MPESA_PASSKEY: STK Push passkey (from Safaricom registration)
          - MPESA_CALLBACK_URL: URL for STK Push callbacks
          - MPESA_CONFIRMATION_URL: URL for C2B confirmations
          - MPESA_VALIDATION_URL: URL for C2B validation (optional)
        """
        settings = get_settings()

        self.env = getattr(settings, "mpesa_env", "sandbox")
        self.consumer_key = settings.mpesa_consumer_key.get_secret_value()
        self.consumer_secret = settings.mpesa_consumer_secret.get_secret_value()
        self.shortcode = getattr(settings, "mpesa_shortcode", "")
        self.passkey = settings.mpesa_passkey.get_secret_value()
        self.callback_url = getattr(settings, "mpesa_callback_url", "")
        self.confirmation_url = getattr(settings, "mpesa_confirmation_url", "")
        self.validation_url = getattr(settings, "mpesa_validation_url", "")

        # Get the URL set for this environment
        self._urls = _DARAJA_URLS[self.env]

        # OAuth token cache: (token, expiry_timestamp)
        # We cache the token and refresh it before expiry
        self._token_cache: tuple[Optional[str], float] = (None, 0.0)

        # HTTP client (lazy-initialized — httpx.AsyncClient for async calls)
        self._http_client: Optional[httpx.AsyncClient] = None

    # ── HTTP Client ─────────────────────────────────────────────

    async def _get_http_client(self) -> httpx.AsyncClient:
        """
        Get or create the async HTTP client.

        Teaching note: We use httpx (not requests) because it supports
        async natively. We reuse the client connection across calls
        for efficiency (connection pooling).
        """
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,  # 30-second timeout for rural network reliability
                verify=True,  # always verify TLS (production security)
            )
        return self._http_client

    # ── OAuth Token ──────────────────────────────────────────────

    async def get_oauth_token(self) -> str:
        """
        Get the Safaricom OAuth access token for Daraja API authentication.

        Daraja API uses OAuth 2.0 with HTTP Basic Auth for token
        retrieval. The token is valid for ~60 minutes. We cache it
        and refresh 5 minutes before expiry.

        Returns:
            Access token string (to be used as Bearer token)

        Teaching note: OAuth tokens are obtained by making a GET request
        to the OAuth endpoint with HTTP Basic Auth using your Consumer
        Key and Consumer Secret. The response contains an `access_token`
        and `expires_in` (seconds until expiry).
        """
        # Check cache — refresh if token expires in < 5 minutes
        token, expiry = self._token_cache
        if token and time.time() < (expiry - 300):
            return token

        # Generate Basic Auth header from consumer key + secret
        # Basic Auth = base64(consumer_key:consumer_secret)
        credentials = f"{self.consumer_key}:{self.consumer_secret}"
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")

        client = await self._get_http_client()
        try:
            response = await client.get(
                self._urls["oauth"],
                headers={
                    "Authorization": f"Basic {encoded}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()

            data = response.json()
            token = data["access_token"]
            expires_in = data.get("expires_in", 3600)  # default 1 hour
            expiry = time.time() + expires_in

            # Cache the token
            self._token_cache = (token, expiry)
            return token

        except Exception as exc:
            raise RuntimeError(f"Failed to get M-Pesa OAuth token: {exc}") from exc

    # ── STK Push ──────────────────────────────────────────────────

    async def trigger_stk_push(
        self,
        phone: str,
        amount: float,
        account_ref: str,
        transaction_desc: str = "Payment",
    ) -> dict[str, Any]:
        """
        Trigger an STK Push (Lipa na M-Pesa Online) payment prompt.

        This sends a payment request to the customer's phone. The customer
        sees an M-Pesa PIN prompt and enters their PIN to authorize.

        Args:
            phone: Customer's phone number in E.164 or local format
                   (e.g., "+254****5678" or "0712345678")
            amount: Amount in KES (float or int)
            account_ref: Reference for the transaction (e.g., customer ID,
                         invoice number, order number). Max 12 chars.
            transaction_desc: Short description (max 13 chars).

        Returns:
            Daraja API response dict, containing:
              - MerchantRequestID: our request ID
              - CheckoutRequestID: used to query status later
              - ResponseCode: "0" = success (prompt sent)
              - ResponseDescription: human-readable status
              - CustomerMessage: message shown to customer

        Raises:
            RuntimeError: if the API call fails

        Teaching note: The STK Push "Password" parameter is computed as:
            Base64(Shortcode + Passkey + Timestamp)
        The timestamp format is: YYYYMMDDHHmmss
        This is a Safaricom-specific authentication mechanism to prevent
        replay attacks on STK Push requests.
        """
        # Get OAuth token
        token = await self.get_oauth_token()

        # Normalize phone number for M-Pesa (format: 2547XXXXXXXXX, no "+")
        normalized_phone = self._normalize_phone_for_mpesa(phone)

        # Generate timestamp: YYYYMMDDHHmmss
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        # Generate password: Base64(Shortcode + Passkey + Timestamp)
        # This is Safaricom's way of authenticating STK Push requests
        password_str = f"{self.shortcode}{self.passkey}{timestamp}"
        password = base64.b64encode(password_str.encode("utf-8")).decode("utf-8")

        # Build the STK Push request payload
        payload = {
            "BusinessShortCode": self.shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",  # or "BuyGoodsOnline" for till
            "Amount": int(amount),  # M-Pesa requires integer amounts
            "PartyA": normalized_phone,  # customer phone
            "PartyB": self.shortcode,     # business shortcode
            "PhoneNumber": normalized_phone,
            "CallBackURL": self.callback_url,
            "AccountReference": account_ref[:12],  # max 12 chars
            "TransactionDesc": transaction_desc[:13],  # max 13 chars
        }

        client = await self._get_http_client()
        try:
            response = await client.post(
                self._urls["stk_push"],
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as exc:
            # Parse error response from Daraja
            error_body = {}
            try:
                error_body = exc.response.json()
            except Exception:
                pass
            raise RuntimeError(
                f"M-Pesa STK Push failed: HTTP {exc.response.status_code}: "
                f"{error_body.get('errorMessage', str(exc))}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"M-Pesa STK Push failed: {exc}") from exc

    async def query_stk_status(self, checkout_request_id: str) -> dict[str, Any]:
        """
        Query the status of an STK Push request.

        Used when we haven't received a callback (e.g., network outage
        prevented the callback from reaching us).

        Args:
            checkout_request_id: The CheckoutRequestID from the STK Push response

        Returns:
            Daraja API response with ResultCode, ResultDescription, etc.
        """
        token = await self.get_oauth_token()
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        password_str = f"{self.shortcode}{self.passkey}{timestamp}"
        password = base64.b64encode(password_str.encode("utf-8")).decode("utf-8")

        payload = {
            "BusinessShortCode": self.shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }

        client = await self._get_http_client()
        try:
            response = await client.post(
                self._urls["stk_query"],
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"M-Pesa STK query failed: {exc}") from exc

    # ── C2B Registration ─────────────────────────────────────────

    async def register_c2b_urls(self) -> dict[str, Any]:
        """
        Register C2B validation and confirmation URLs with Safaricom.

        This tells Safaricom where to send C2B webhooks. Must be called
        once when setting up the shortcode (or when changing URLs).

        Returns:
            Daraja API response dict

        Teaching note: C2B has two webhook types:
        - Validation URL: called in real-time when a customer initiates
          a C2B payment. You can approve or reject the payment (e.g.,
          reject if the amount doesn't match an expected invoice).
        - Confirmation URL: called after the payment is completed.
          This is the authoritative record of the payment.
        """
        token = await self.get_oauth_token()

        payload = {
            "ShortCode": self.shortcode,
            "ResponseType": "Completed",  # or "Cancelled" to auto-reject
            "ConfirmationURL": self.confirmation_url,
            "ValidationURL": self.validation_url,
        }

        client = await self._get_http_client()
        try:
            response = await client.post(
                self._urls["c2b_register"],
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"C2B URL registration failed: {exc}") from exc

    # ── Webhook Validation ────────────────────────────────────────

    async def validate_mpesa_webhook(
        self,
        body: bytes,
        headers: dict[str, str],
        client_ip: Optional[str] = None,
    ) -> bool:
        """
        Validate that an M-Pesa webhook callback is authentic.

        Safaricom does NOT cryptographically sign M-Pesa webhooks (unlike
        Twilio's HMAC). Instead, we validate using:

        1. HTTPS transport security (TLS ensures the connection is authentic)
        2. BusinessShortCode in the payload matches our configured shortcode
        3. IP allowlisting (optional — Safaricom publishes their gateway IPs)

        Args:
            body: raw request body bytes
            headers: HTTP headers from the request
            client_ip: client IP address (for IP allowlist check)

        Returns:
            True if the webhook is authentic, False otherwise

        Teaching note: This is weaker than Twilio's HMAC validation.
        For production, ALWAYS use HTTPS and configure IP allowlisting
        if Safaricom provides their gateway IPs. The BusinessShortCode
        check prevents spoofing from other M-Pesa accounts.
        """
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False

        # Check 1: BusinessShortCode matches our configured shortcode
        # The payload contains this in different locations depending on
        # whether it's an STK callback or C2B confirmation.
        shortcode = (
            # STK Push callback: BusinessShortCode is in stkCallback
            payload.get("Body", {})
            .get("stkCallback", {})
            .get("BusinessShortCode")
            # C2B confirmation: BusinessShortCode is at top level
            or payload.get("BusinessShortCode")
            # C2B validation: same as confirmation
            or payload.get("BusinessShortCode")
        )

        if shortcode and str(shortcode) != str(self.shortcode):
            # Shortcode mismatch — this webhook is not for our account
            return False

        # Check 2: IP allowlist (if configured)
        # Safaricom publishes their API gateway IPs for allowlisting.
        # In production, configure these and validate.
        # For now, we accept if the shortcode matches.
        # TODO: Add IP allowlist check when Safaricom IPs are known

        return True

    # ── Parse STK Push Callback ──────────────────────────────────

    def parse_stk_callback(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Parse an STK Push result callback from Safaricom.

        Safaricom sends a POST to the CallBackURL after the customer
        responds to the STK Push prompt (either enters PIN or cancels).

        Payload structure:
        {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": "29115-34620561-1",
                    "CheckoutRequestID": "ws_CO_13012020032352465579",
                    "ResultCode": 0,          # 0 = success
                    "ResultDesc": "The service request is processed successfully.",
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": 1.00},
                            {"Name": "MpesaReceiptNumber", "Value": "SIH4XXXXXX"},
                            {"Name": "Balance", "Value": "0.00"},  # optional
                            {"Name": "TransactionDate", "Value": 20200113032352},
                            {"Name": "PhoneNumber", "Value": 254****5678}
                        ]
                    }
                }
            }
        }

        Args:
            payload: The parsed JSON payload from the webhook

        Returns:
            Normalized dict with:
              - result_code: int (0 = success)
              - result_desc: str
              - merchant_request_id: str
              - checkout_request_id: str
              - amount: Optional[float] (KES)
              - mpesa_receipt_number: Optional[str]
              - transaction_date: Optional[str] (ISO format)
              - phone: Optional[str] (E.164 format)
              - is_successful: bool

        Teaching note: If ResultCode != 0, there's no CallbackMetadata
        (the payment failed). We still parse what we can for logging.
        """
        stk = payload.get("Body", {}).get("stkCallback", {})

        result_code = stk.get("ResultCode")
        result_desc = stk.get("ResultDesc", "")
        merchant_request_id = stk.get("MerchantRequestID", "")
        checkout_request_id = stk.get("CheckoutRequestID", "")

        # Parse CallbackMetadata items (only present on success)
        metadata_items = (
            stk.get("CallbackMetadata", {})
            .get("Item", [])
        )

        # Convert items list to a dict: [{"Name": "Amount", "Value": 1.00}, ...]
        # → {"Amount": 1.00, "MpesaReceiptNumber": "SIH4XXXXXX", ...}
        metadata = {}
        for item in metadata_items:
            name = item.get("Name")
            value = item.get("Value")
            if name and value is not None:
                metadata[name] = value

        # Extract key fields
        amount = metadata.get("Amount")
        mpesa_receipt = metadata.get("MpesaReceiptNumber")
        transaction_date = metadata.get("TransactionDate")
        phone = metadata.get("PhoneNumber")

        # Format transaction date if present (M-Pesa sends YYYYMMDDHHmmss as int)
        formatted_date = None
        if transaction_date:
            try:
                # Parse "20200113032352" → "2020-01-13T03:23:52"
                date_str = str(transaction_date)
                formatted_date = (
                    f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                    f"T{date_str[8:10]}:{date_str[10:12]}:{date_str[12:14]}"
                )
            except (IndexError, ValueError):
                pass

        return {
            "result_code": result_code,
            "result_desc": result_desc,
            "merchant_request_id": merchant_request_id,
            "checkout_request_id": checkout_request_id,
            "amount": float(amount) if amount else None,
            "mpesa_receipt_number": mpesa_receipt,
            "transaction_date": formatted_date,
            "phone": f"+{phone}" if phone else None,  # normalize to E.164
            "is_successful": result_code == MPESA_SUCCESS_CODE,
            "is_retryable": result_code in MPESA_RETRYABLE_CODES,
        }

    # ── Parse C2B Confirmation ───────────────────────────────────

    def parse_c2b_confirmation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Parse a C2B (Customer-to-Business) payment confirmation webhook.

        This is sent by Safaricom AFTER a customer has completed a C2B
        payment (sent money to the business Paybill/Till number).

        Payload structure:
        {
            "TransactionType": "Pay Bill",
            "TransID": "SIH4XXXXXX",
            "TransTime": "20200113032352",
            "TransAmount": "100.00",
            "BusinessShortCode": "123456",
            "BillRefNumber": "CUST001",      # account reference
            "InvoiceNumber": "",
            "OrgAccountBalance": "5000.00",
            "ThirdPartyTransID": "",
            "MSISDN": "254****5678",          # customer phone
            "FirstName": "John",
            "MiddleName": "M",
            "LastName": "Doe"
        }

        Args:
            payload: The parsed JSON payload from the webhook

        Returns:
            Normalized dict with transaction details:
              - transaction_id: M-Pesa transaction code (e.g., "SIH4XXXXXX")
              - transaction_type: "Pay Bill" or "Buy Goods"
              - transaction_time: ISO format datetime
              - amount: float (KES)
              - business_short_code: str
              - account_reference: str (customer reference)
              - customer_phone: str (E.164 format)
              - customer_name: str (full name)
              - is_successful: True (C2B confirmations are always successful)

        Teaching note: C2B confirmation webhooks are only sent for
        COMPLETED payments. If a payment fails, you get no confirmation.
        The validation webhook (separate) lets you approve/reject in
        real-time before the payment completes.
        """
        trans_time = payload.get("TransTime", "")

        # Format transaction time (YYYYMMDDHHmmss → ISO)
        formatted_time = None
        if trans_time:
            try:
                time_str = str(trans_time)
                formatted_time = (
                    f"{time_str[:4]}-{time_str[4:6]}-{time_str[6:8]}"
                    f"T{time_str[8:10]}:{time_str[10:12]}:{time_str[12:14]}"
                )
            except (IndexError, ValueError):
                pass

        # Build customer full name from parts
        first_name = payload.get("FirstName", "")
        middle_name = payload.get("MiddleName", "")
        last_name = payload.get("LastName", "")
        full_name = " ".join(filter(None, [first_name, middle_name, last_name])).strip()

        # Parse amount (comes as string, e.g., "100.00")
        amount_str = payload.get("TransAmount", "0")
        try:
            amount = float(amount_str)
        except (ValueError, TypeError):
            amount = 0.0

        # Normalize phone to E.164
        phone = payload.get("MSISDN", "")
        if phone and not phone.startswith("+"):
            phone = f"+{phone}"

        return {
            "transaction_id": payload.get("TransID", ""),
            "transaction_type": payload.get("TransactionType", ""),
            "transaction_time": formatted_time,
            "amount": amount,
            "business_short_code": payload.get("BusinessShortCode", ""),
            "account_reference": payload.get("BillRefNumber", ""),
            "customer_phone": phone,
            "customer_name": full_name,
            "is_successful": True,  # C2B confirmations are always successful
            "raw_payload": payload,
        }

    # ── Phone Number Normalization ───────────────────────────────

    def _normalize_phone_for_mpesa(self, phone: str) -> str:
        """
        Normalize phone number for M-Pesa API (format: 254XXXXXXXXX).

        Accepts:
          - "+254****5678" → "254****5678"
          - "0712345678"   → "254712345678"
          - "254****5678"  → "254****5678" (already correct)

        Returns:
            Phone number in M-Pesa format (254XXXXXXXXX, no "+")
        """
        # Strip leading "+"
        cleaned = phone.lstrip("+")

        # Convert local format (0712345678) to international (254712345678)
        if cleaned.startswith("0"):
            cleaned = "254" + cleaned[1:]

        return cleaned

    # ── Cleanup ──────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the HTTP client. Call on application shutdown."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


# ── Factory ──────────────────────────────────────────────────────

def get_mpesa_adapter() -> MpesaAdapter:
    """Factory: returns a configured M-Pesa adapter."""
    return MpesaAdapter()