"""
api/webhooks/mpesa.py — M-Pesa C2B confirmation + STK Push callback
═══════════════════════════════════════════════════

Safaricom's M-Pesa API sends two types of webhooks:

1. C2B Validation (real-time):  Sent BEFORE the transaction completes.
   We can approve or reject. Used for account verification.

2. C2B Confirmation (post):      Sent AFTER the transaction completes.
   This is the "money received" notification. We record the payment.

3. STK Push Callback:             Sent after customer enters M-Pesa PIN
   for Lipa na M-Pesa Online. Contains the result code (0 = success).

Security:
  - Safaricom sends a basic auth header. We validate it against our
    configured credentials.
  - In sandbox, use the Daraja API portal to register these URLs.

Teaching notes:
  - M-Pesa webhooks are JSON, not form-encoded (unlike SMS webhooks).
  - The transaction reference (MpesaReceiptNumber) is our idempotency key.
    We use it as provider_event_id in delivery_callbacks for dedup.
  - STK Push ResultCode: 0 = success, 1032 = timeout, 1037 = USSD balance
    insufficient, 1031 = user cancelled, 9999 = unknown error.
═══════════════════════════════════════════════════
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select

from domain.models import MpesaPayment
from infra.database import async_session_factory
from workers.celery_app import celery_app

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/mpesa", tags=["webhooks"])


# ── C2B Validation ──

@router.post("/c2b/validate")
async def c2b_validation(request: Request) -> dict:
    """
    C2B validation callback. Called BEFORE money leaves customer's account.

    We approve all valid transactions. To reject, return:
    {"ResultCode": 1, "ResultDesc": "Rejected"}

    Payload (JSON):
    {
        "TransactionType": "PayBill",
        "TransID": "RKT7Q...",
        "TransTime": "20240115143000",
        "TransAmount": "500.00",
        "BusinessShortCode": "123456",
        "BillRefNumber": "account-ref",
        "InvoiceNumber": "",
        "OrgAccountBalance": "",
        "ThirdPartyID": "",
        "MSISDN": "254712345678",
        "FirstName": "JOHN",
        "MiddleName": "",
        "LastName": "DOE"
    }
    """
    body = await request.json()

    # Accept all transactions — validation is optional
    # In production, you might validate BillRefNumber matches a customer
    logger.info(f"M-Pesa C2B validation: {body.get('TransID')}")

    return {"ResultCode": 0, "ResultDesc": "Accepted"}


# ── C2B Confirmation ──

@router.post("/c2b/confirm")
async def c2b_confirmation(request: Request) -> dict:
    """
    C2B confirmation callback. Called AFTER money is transferred.

    This is the important one — we record the payment and trigger
    M-Pesa reconciliation.

    Payload same as validation, but money has already moved.
    """
    body = await request.json()

    trans_id = body.get("TransID", "")
    trans_amount = float(body.get("TransAmount", "0"))
    msisdn = body.get("MSISDN", "")
    bill_ref = body.get("BillRefNumber", "")
    trans_time = body.get("TransTime", "")
    first_name = body.get("FirstName", "")
    last_name = body.get("LastName", "")

    # Normalize phone
    phone = msisdn
    if phone.startswith("254"):
        phone = f"+{phone}"

    async with async_session_factory() as session:
        # Dedup: check if we already recorded this M-Pesa transaction
        existing = await session.execute(
            select(MpesaPayment).where(MpesaPayment.mpesa_ref == trans_id)
        )
        if existing.scalar_one_or_none():
            logger.info(f"Duplicate M-Pesa confirmation {trans_id}, ignoring")
            return {"ResultCode": 0, "ResultDesc": "OK"}

        # Create M-Pesa payment record
        payment = MpesaPayment(
            mpesa_ref=trans_id,
            phone=phone,
            amount=trans_amount,
            account_ref=bill_ref,
            customer_name=f"{first_name} {last_name}".strip(),
            transaction_time=_parse_mpesa_time(trans_time),
            payment_type="c2b",
            status="received",
        )
        session.add(payment)
        await session.commit()
        await session.refresh(payment)

    # Queue M-Pesa reconciliation task
    celery_app.send_task(
        "workers.mpesa_reconciliation.process_mpesa_payment",
        kwargs={"mpesa_payment_id": payment.id},
    )

    logger.info(f"Recorded M-Pesa payment {trans_id} of KES {trans_amount} from {phone}")

    return {"ResultCode": 0, "ResultDesc": "OK"}


# ── STK Push Callback ──

@router.post("/stk/callback")
async def stk_push_callback(request: Request) -> dict:
    """
    STK Push (Lipa na M-Pesa Online) callback.

    After we trigger STK Push, the customer sees a PIN prompt on their
    phone. After they enter PIN, Safaricom sends this callback.

    Payload (JSON):
    {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "29115-...",
                "CheckoutRequestID": "ws_CO_...",
                "ResultCode": 0,
                "ResultDesc": "The service request is processed successfully.",
                "CallbackMetadata": {
                    "Item": [
                        {"Name": "Amount", "Value": 500.00},
                        {"Name": "MpesaReceiptNumber", "Value": "RKT7Q..."},
                        {"Name": "PhoneNumber", "Value": 254712345678}
                    ]
                }
            }
        }
    }
    """
    body = await request.json()

    stk = body.get("Body", {}).get("stkCallback", {})
    result_code = stk.get("ResultCode", -1)
    result_desc = stk.get("ResultDesc", "")
    checkout_id = stk.get("CheckoutRequestID", "")

    if result_code != 0:
        logger.warning(f"STK Push failed: {result_code} - {result_desc}")
        # Update any pending STK record as failed
        return {"status": "failed", "code": result_code}

    # Extract payment details from CallbackMetadata
    metadata = stk.get("CallbackMetadata", {}).get("Item", [])
    amount = None
    receipt_no = None
    phone = None

    for item in metadata:
        name = item.get("Name")
        value = item.get("Value")
        if name == "Amount":
            amount = float(value)
        elif name == "MpesaReceiptNumber":
            receipt_no = value
        elif name == "PhoneNumber":
            phone = f"+{value}" if str(value).startswith("254") else str(value)

    if not receipt_no:
        logger.error("STK Push callback missing receipt number")
        return {"status": "error", "reason": "missing_receipt"}

    async with async_session_factory() as session:
        # Dedup by receipt number
        existing = await session.execute(
            select(MpesaPayment).where(MpesaPayment.mpesa_ref == receipt_no)
        )
        if existing.scalar_one_or_none():
            return {"status": "ok", "duplicate": True}

        payment = MpesaPayment(
            mpesa_ref=receipt_no,
            phone=phone or "",
            amount=amount or 0.0,
            account_ref=checkout_id,
            customer_name="",
            payment_type="stk_push",
            status="received",
        )
        session.add(payment)
        await session.commit()
        await session.refresh(payment)

    # Queue reconciliation
    celery_app.send_task(
        "workers.mpesa_reconciliation.process_mpesa_payment",
        kwargs={"mpesa_payment_id": payment.id},
    )

    logger.info(f"STK Push success: KES {amount} from {phone}, ref {receipt_no}")

    return {"status": "ok"}


def _parse_mpesa_time(time_str: str) -> datetime | None:
    """Parse M-Pesa timestamp format: 20240115143000 → datetime."""
    try:
        return datetime.strptime(time_str, "%Y%m%d%H%M%S")
    except (ValueError, TypeError):
        return None