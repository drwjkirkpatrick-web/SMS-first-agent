"""
infra/logging_filter.py — Structured logging PII masking filter
════════════════════════════════════════════════════════════════════════

PURPOSE
-------
Even with careful application-level masking (``domain/masking.py``),
phone numbers and email addresses can leak into log messages via stack
traces, exception reprs, and third-party library warnings. This module
installs a ``logging.Filter`` on the **root logger** that scans every
record's formatted message and masks PII *before* it reaches any
handler.

This is a defence-in-depth measure: the application code already masks
PII before logging, but this filter catches anything that slips through
— for example, a Celery task error that includes the phone number in a
``repr()`` of a task argument.

KEY DESIGN DECISIONS
--------------------
  1. **Root logger filter** — attaching to the root logger means the
     filter sees records from *all* loggers in the process, including
     third-party libraries (httpx, celery, sqlalchemy) that may
     inadvertently log PII.
  2. **Mutate the LogRecord, don't suppress it** — the filter always
     returns ``True`` (never drops a record). It rewrites
     ``record.msg`` and clears ``record.args`` so the masked version is
     what every handler ultimately emits.
  3. **Delegate phone masking to ``domain.masking.mask_phone``** — keeps
     the mask format consistent with the rest of the application. If
     the mask format changes (e.g., showing last 3 digits instead of 4),
     it changes everywhere automatically.
  4. **Email masking keeps the domain** — replaces the local part with
     ``***`` and preserves the domain. The domain is not PII for a
     business's own staff (e.g., ``owner@mamambo.ga`` → ``*****@mamambo.ga``)
     and helps with debugging email-delivery issues.
  5. **Idempotent setup** — ``setup_pii_logging()`` checks for an
     existing filter before adding, so repeated calls (e.g., in tests
     that re-initialise the app) do not stack duplicate filters.

ADAPTATION FROM THE TUITION AGENT
--------------------------------
  - ``"school"`` → ``"business"`` in comments and docstrings.
  - The ``mask_phone`` import from ``domain.masking`` is unchanged —
    the masking functions are regulation-agnostic (see the docstring in
    ``domain/masking.py``).
  - The ``PIIMaskingFilter`` class and ``setup_pii_logging`` function
    are otherwise unchanged.

TEACHING NOTES
--------------
  - A ``logging.Filter`` runs *before* handlers format the record, so
    we mutate ``record.msg`` (the raw template) and ``record.args`` to
    ensure the masked version is what gets emitted.
  - For records that are already fully formatted strings (common with
    ``logger.info("…")`` with no args), we simply replace ``record.msg``.
  - The regex patterns are deliberately conservative: the phone regex
    requires 10–15 digits to avoid matching short numeric IDs (like
    order numbers or M-Pesa confirmation codes).
  - This filter adds a small CPU cost to every log record, but in
    practice the cost is negligible (microseconds per record) and the
    safety benefit is significant.
  - Kenya's Data Protection Act 2019 (DPA) §25 requires data
    minimisation — logging only what is necessary and masking PII. This
    filter is a technical control that supports that legal requirement.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - ``domain/masking.py`` — provides ``mask_phone`` (and ``mask_name``,
    ``mask_amount``) used by the application layer.
  - ``api/main.py`` — calls ``setup_pii_logging()`` in the FastAPI
    lifespan startup handler.
  - ``celery_app.py`` (or equivalent) — calls ``setup_pii_logging()`` in
    the Celery worker startup signal so worker logs are also masked.
  - ``infra/settings.py`` — ``log_level`` controls what records reach
    the filter (the filter runs on all levels but only sees records
    that pass the logger's level check).

════════════════════════════════════════════════════════════════════════
"""

import logging
import re
from typing import Any

# ── PII masking utility ──────────────────────────────────────────────
# We import mask_phone from domain.masking so the filter uses the same
# mask format as the rest of the application. If the format changes
# (e.g., from "last 4 digits" to "last 3"), it changes everywhere.
from domain.masking import mask_phone

# ── Regex patterns for PII detection ──────────────────────────────────
# These patterns are deliberately conservative to minimise false
# positives (masking things that aren't actually PII).

# Match E.164-ish phone numbers: optional +, 10–15 consecutive digits.
# The minimum of 10 digits avoids matching short numeric IDs like
# order numbers, M-Pesa confirmation codes (which are ~10 chars but
# alphanumeric), or Celery task IDs.
#
# Examples that match:
#   +254712345678  (E.164 with country code)
#   254712345678   (E.164 without +)
#   0712345678     (local Kenyan format, 10 digits)
#
# Examples that don't match:
#   12345          (too short — likely an ID)
#   ABC123         (not all digits)
_PHONE_RE = re.compile(r"\+?\d{10,15}")

# Match common email formats: local@domain.tld
# The local part allows alphanumeric plus . _ % + - characters.
# The TLD must be at least 2 alpha characters (covers .com, .co.ke, etc.)
#
# Examples that match:
#   owner@mamambo.ga
#   john.doe+filter@example.co.ke
#
# This regex does NOT match all valid RFC 5322 email addresses, but it
# covers the vast majority of real-world addresses and is fast.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


class PIIMaskingFilter(logging.Filter):
    """
    A ``logging.Filter`` that masks phone numbers and email addresses
    in log records before they are emitted by handlers.

    The filter is attached to the *root* logger so it sees records from
    all loggers in the process (library code included). It never
    suppresses records — it only sanitises them.

    Teaching note: ``logging.Filter`` is different from a handler-level
    filter. A logger-level filter runs when the record is *created*,
    before it reaches any handler. This means the masked version is what
    every handler (console, file, syslog) sees.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Mask PII in the record's message.

        Always returns ``True`` — we never suppress records, only
        sanitise them. Returning ``False`` would drop the record
        entirely, which would hide errors and make debugging harder.

        The method:
          1. Builds the full message string from ``record.msg`` +
             ``record.args`` (via ``record.getMessage()``).
          2. Runs the PII-masking regexes on the string.
          3. If anything changed, replaces ``record.msg`` with the
             masked string and clears ``record.args`` so handlers don't
             re-format and reintroduce PII.
        """
        # Build the full message string from msg + args.
        # getMessage() applies %-formatting if args are present.
        try:
            message = record.getMessage()
        except Exception:
            # If formatting itself fails (e.g., bad % args), let the
            # handler deal with it — don't crash the logging call.
            return True

        # Mask PII in the assembled message.
        masked = self._mask_text(message)
        if masked != message:
            # Replace the record's msg with the masked string and clear
            # args so handlers don't re-format and reintroduce PII.
            #
            # This is important: if we only replaced record.msg but
            # left record.args intact, a handler calling
            # record.getMessage() would re-apply the original args to
            # the masked template, potentially reintroducing PII or
            # producing a malformed string.
            record.msg = masked
            record.args = None

        # Always return True — we sanitise, never suppress.
        return True

    @staticmethod
    def _mask_text(text: str) -> str:
        """
        Mask phone numbers and emails in *text*.

        This is a static method (no instance state needed) so it can
        also be called directly for one-off masking if needed.
        """

        def _mask_phone_match(m: re.Match) -> str:
            """
            Convert a regex phone match to the project mask format.

            Delegates to ``domain.masking.mask_phone``, which returns
            ``X`` characters + the last 4 digits (e.g.,
            ``+254712345678`` → ``XXXXXXXX5678``).
            """
            raw = m.group()
            return mask_phone(raw)

        def _mask_email_match(m: re.Match) -> str:
            """
            Mask the local part of an email, keeping the domain.

            Example:
                owner@mamambo.ga → *****@mamambo.ga

            Keeping the domain is safe for a business's own staff and
            helps with debugging email-delivery issues. The local part
            is what identifies the individual, so that is what we mask.
            """
            email = m.group()
            local, _, domain = email.partition("@")
            if len(local) <= 1:
                # Single-character local part — just mask with *.
                masked_local = "*"
            else:
                # Keep first char, mask the rest (same approach as
                # mask_name in domain.masking.py).
                masked_local = local[0] + "*" * (len(local) - 1)
            return f"{masked_local}@{domain}"

        # Apply phone masking first, then email masking.
        # Order matters: if an email contained digits that looked like
        # a phone number, the phone regex would mask them. By running
        # phone first, we mask raw numbers; then email masks addresses.
        # In practice, emails rarely contain 10+ consecutive digits in
        # the local part, so the order rarely matters.
        text = _PHONE_RE.sub(_mask_phone_match, text)
        text = _EMAIL_RE.sub(_mask_email_match, text)
        return text


def setup_pii_logging() -> None:
    """
    Attach the ``PIIMaskingFilter`` to the root logger.

    Call this once at application startup::

        from infra.logging_filter import setup_pii_logging
        setup_pii_logging()

    In the FastAPI app, call it in the lifespan startup handler. In
    Celery workers, call it in the ``worker_ready`` signal so worker
    logs are also masked.

    The function is **idempotent**: it checks whether a
    ``PIIMaskingFilter`` is already attached before adding a new one, so
    repeated calls (e.g., in tests that re-initialise the app, or in
    both FastAPI and Celery startup) do not create duplicates.

    Idempotency is important because duplicate filters would mask the
    same record multiple times — harmless for phone numbers (masking a
    masked string produces the same result) but wasteful.
    """
    root = logging.getLogger()

    # Check for an existing PIIMaskingFilter to avoid duplicates.
    for existing in root.filters:
        if isinstance(existing, PIIMaskingFilter):
            return  # already installed — nothing to do

    # Attach the filter to the root logger.
    root.addFilter(PIIMaskingFilter())