"""
domain/masking.py — PII masking utilities
═══════════════════════════════════════════════════════════════════════

INHERITED from the tuition agent (domain/masking.py).
The masking logic is regulation-agnostic — it protects PII regardless
of whether the governing law is FERPA or Kenya Data Protection Act.

PURPOSE
-------
Every log message, error report, and metric label that could contain
sensitive data (phone numbers, names, amounts) must pass through
these functions. This implements the data minimization principle
required by Kenya DPA 2019 §25.

TEACHING NOTES
--------------
  - `mask_phone` shows only the last 4 digits (industry standard).
  - `mask_name` shows only the first letter (prevents full name leakage
    in log aggregation systems like Loki/ELK).
  - `mask_amount` always shows "KES XXX.XX" — never reveals even partial
    digits. Full amounts live only in the database audit_events table
    (which has its own access controls).
  - These functions are synchronous (no DB access) and safe to call
    from any context — including signal handlers and crash dumps.
═══════════════════════════════════════════════════════════════════════
"""


def mask_phone(phone: str | None) -> str:
    """
    Mask a phone number, showing only the last 4 digits.

    Examples:
      +254712345678 → XXXXXXXX5678
      0712-345-678  → XXXXXXX678
    """
    if not phone:
        return "XXXX"
    if len(phone) >= 4:
        return "X" * (len(phone) - 4) + phone[-4:]
    return "X" * len(phone)


def mask_name(name: str | None) -> str:
    """
    Mask a name, showing only the first letter.

    Examples:
      Wanjiru → W******
      Li      → L*
    """
    if not name:
        return "*"
    if len(name) > 1:
        return name[0] + "*" * (len(name) - 1)
    return name[0] if name else "*"


def mask_amount(amount: float | None) -> str:
    """
    Mask a KES amount in logs (always show "KES XXX.XX").
    Full amounts live only in the database audit_events table.
    """
    if amount is None:
        return "KES XXX.XX"
    return "KES XXX.XX"  # always masked; never show even partial digits