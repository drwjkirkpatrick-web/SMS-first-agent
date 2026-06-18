"""
domain/templates.py — Bilingual EN/SW SMS templates with placeholder rendering
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Defines every SMS message template the platform can send, in BOTH
English and Swahili. The TemplateRenderer selects the right language
based on the customer's `preferred_language` field.

KEY DESIGN DECISIONS
--------------------
  1. Bilingual: every template has `en` and `sw` variants. Kenya is
     officially bilingual (Swahili national, English official).
  2. Currency: KES (Kenya Shillings). Templates use "KES" prefix.
  3. Opt-out footer: marketing templates append "Reply STOP to opt out"
     per Kenya DPA 2019 + Communications Authority guidelines.
  4. Segment counting: GSM-7 / UCS-2 detection (inherited from tuition
     agent) to enforce max segments and control cost.
  5. Safe rendering: `str.format()` — no code execution. Missing keys
     fall back to empty string (graceful degradation).

TEMPLATE LIST
-------------
  reminder_due_14, reminder_due_3, reminder_due_today, reminder_late,
  payment_confirmed, callback_ack, credit_terms_ack, status_reply,
  help_reply, opt_out_confirm, opt_in_confirm, promo_message,
  loyalty_points, book_appointment, business_hours, business_location.

TEACHING NOTES
--------------
  - Templates are deterministic: same inputs → same output.
  - We check character count to enforce max segments (default 2 = 306 chars
    GSM-7, 134 chars UCS-2).
  - The `render()` function handles missing placeholders gracefully.
  - Quiet hours are enforced at SEND time (policy_service), not template time.
  - Swahili translations are written for natural SMS length (concise).
  - The GSM-7 charset includes basic Latin + a few accented chars.
    Swahili uses standard Latin letters (no special diacritics beyond
    what GSM-7 covers), so most SW templates fit in 1–2 segments.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - `domain/reminder_service.py` returns a `body_template` name; the
    send worker calls `TemplateRenderer.render(name, context, language)`.
  - `domain/campaign_service.py` uses "promo_message" template.
  - `domain/mpesa_service.py` uses "payment_confirmed" after M-Pesa webhook.
  - `infra/settings.py` provides `max_sms_segments` (default 2).
═══════════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass
from typing import Optional

from infra.settings import get_settings


@dataclass(frozen=True)
class MessageTemplate:
    """
    A template with metadata.

    `bodies` holds both language variants: {"en": "...", "sw": "..."}.
    `max_segments` caps the segment count (default 2 = 306 GSM-7 chars).
    """
    name: str
    bodies: dict[str, str]   # {"en": "...", "sw": "..."}
    max_segments: int = 2


# ═══════════════════════════════════════════════════════════════
# Template Library (bilingual EN / SW)
# ═══════════════════════════════════════════════════════════════
#
# Placeholders use Python str.format() style: {business_name}, {amount_due}, etc.
# Every template MUST have both "en" and "sw" keys.

TEMPLATES: dict[str, MessageTemplate] = {

    # ── Reminder cadence (inherited from tuition agent, adapted) ──

    "reminder_due_14": MessageTemplate(
        name="reminder_due_14",
        bodies={
            "en": "Hi {contact_name}, {business_name} reminder: your balance of KES {amount_due} is due {due_date}. Questions? Reply HELP or CALL. Reply STOP to opt out.",
            "sw": "Habari {contact_name}, {business_name}: salio yako ya KES {amount_due} linapaswa kulipwa {due_date}. Ukiwa na swali, jibu HELP au CALL. Jibu STOP kuacha.",
        },
    ),
    "reminder_due_3": MessageTemplate(
        name="reminder_due_3",
        bodies={
            "en": "Hi {contact_name}, {business_name}: your balance of KES {amount_due} is due in 3 days ({due_date}). Reply HELP for options. Reply STOP to opt out.",
            "sw": "Habari {contact_name}, {business_name}: salio yako ya KES {amount_due} linapaswa kulipwa ndani ya siku 3 ({due_date}). Jibu HELP kwa chaguo. Jibu STOP kuacha.",
        },
    ),
    "reminder_due_today": MessageTemplate(
        name="reminder_due_today",
        bodies={
            "en": "Hi {contact_name}, {business_name}: your balance of KES {amount_due} is due TODAY. Reply CALL to speak with us. Reply STOP to opt out.",
            "sw": "Habari {contact_name}, {business_name}: salio yako ya KES {amount_due} linapaswa kulipwa LEO. Jibu CALL kuzungumza nasi. Jibu STOP kuacha.",
        },
    ),
    "reminder_late": MessageTemplate(
        name="reminder_late",
        bodies={
            "en": "Hi {contact_name}, {business_name}: your balance of KES {amount_due} was due {due_date} and is now overdue. Please reply CALL to discuss. Reply STOP to opt out.",
            "sw": "Habari {contact_name}, {business_name}: salio yako ya KES {amount_due} lilipaswa kulipwa {due_date} na sasa limechelewa. Tafadhali jibu CALL kuzungumza. Jibu STOP kuacha.",
        },
    ),

    # ── Payment confirmation (M-Pesa integration) ──

    "payment_confirmed": MessageTemplate(
        name="payment_confirmed",
        bodies={
            "en": "Hi {contact_name}, {business_name}: Thank you! We've received KES {amount_paid} via {payment_method}. Remaining balance: KES {balance}. Ref: {mpesa_ref}. Reply STOP to opt out.",
            "sw": "Habari {contact_name}, {business_name}: Asante! Tumepokea KES {amount_paid} kupitia {payment_method}. Salio lililobaki: KES {balance}. Ref: {mpesa_ref}. Jibu STOP kuacha.",
        },
    ),

    # ── Acknowledgements ──

    "callback_ack": MessageTemplate(
        name="callback_ack",
        bodies={
            "en": "{business_name}: We received your message and will follow up shortly. Reply STOP to opt out.",
            "sw": "{business_name}: Tumepokea ujumbe wako na tutaifuatilia hivi karibuni. Jibu STOP kuacha.",
        },
    ),
    "credit_terms_ack": MessageTemplate(
        name="credit_terms_ack",
        bodies={
            "en": "{business_name}: Thank you for reaching out. We're reviewing your credit terms request and will contact you within 24 hours. Reply STOP to opt out.",
            "sw": "{business_name}: Asante kwa kuwasiliana. Tunakagua ombi lako la muda wa malipo na tutawasiliana nawe ndani ya saa 24. Jibu STOP kuacha.",
        },
    ),

    # ── Status / help / opt-in-out ──

    "status_reply": MessageTemplate(
        name="status_reply",
        bodies={
            "en": "{business_name} balance for {customer_name}: KES {balance} due {due_date}. Reply PAID if you've submitted payment. Reply STOP to opt out.",
            "sw": "{business_name} salio la {customer_name}: KES {balance} linapaswa {due_date}. Jibu PAID kama umelipa. Jibu STOP kuacha.",
        },
    ),
    "help_reply": MessageTemplate(
        name="help_reply",
        bodies={
            "en": "{business_name} SMS commands: STATUS (balance), PAID (confirm payment), CALL (callback), EXTENSION (credit terms), POINTS (loyalty), PROMO, BOOK, HOURS, LOCATION, STOP (opt out), START (opt in).",
            "sw": "{business_name} amri za SMS: STATUS (salio), PAID (thibitisha malipo), CALL (mpigie), EXTENSION (muda wa malipo), POINTS (pointi), PROMO, BOOK, HOURS, LOCATION, STOP (acha), START (jiunge).",
        },
    ),
    "opt_out_confirm": MessageTemplate(
        name="opt_out_confirm",
        bodies={
            "en": "You've been opted out of {business_name} SMS messages. Reply START to resubscribe.",
            "sw": "Umeachiwa kupokea SMS za {business_name}. Jibu START kujirudisha.",
        },
    ),
    "opt_in_confirm": MessageTemplate(
        name="opt_in_confirm",
        bodies={
            "en": "Welcome back! You're now subscribed to {business_name} SMS messages.",
            "sw": "Karibu tena! Sasa unapokea SMS za {business_name}.",
        },
    ),

    # ── NEW: Promotional campaign message ──

    "promo_message": MessageTemplate(
        name="promo_message",
        bodies={
            "en": "{business_name}: {promo_text} Reply STOP to opt out.",
            "sw": "{business_name}: {promo_text} Jibu STOP kuacha.",
        },
    ),

    # ── NEW: Loyalty points update ──

    "loyalty_points": MessageTemplate(
        name="loyalty_points",
        bodies={
            "en": "Hi {contact_name}, {business_name}: You have {points} loyalty points! Redeem for KES {redeem_value} off your next purchase. Reply STOP to opt out.",
            "sw": "Habari {contact_name}, {business_name}: Una pointi {points} za uaminifu! Zitumie kupata punguzi la KES {redeem_value}. Jibu STOP kuacha.",
        },
    ),

    # ── NEW: Appointment booking (clinic / salon) ──

    "book_appointment": MessageTemplate(
        name="book_appointment",
        bodies={
            "en": "Hi {contact_name}, {business_name}: Your appointment is on {appointment_date}. Reply CALL to reschedule. Reply STOP to opt out.",
            "sw": "Habari {contact_name}, {business_name}: Umti wako ni tarehe {appointment_date}. Jibu CALL kubadilisha. Jibu STOP kuacha.",
        },
    ),

    # ── NEW: Business hours (HOURS keyword reply) ──

    "business_hours": MessageTemplate(
        name="business_hours",
        bodies={
            "en": "{business_name} hours: {hours_text}. We're closed on {closed_days}. Reply STOP to opt out.",
            "sw": "Masaa ya {business_name}: {hours_text}. Tumefunga siku ya {closed_days}. Jibu STOP kuacha.",
        },
    ),

    # ── NEW: Business location (LOCATION keyword reply) ──

    "business_location": MessageTemplate(
        name="business_location",
        bodies={
            "en": "{business_name} is located at {location_text}. Reply STOP to opt out.",
            "sw": "{business_name} iko {location_text}. Jibu STOP kuacha.",
        },
    ),
}


class TemplateRenderer:
    """
    Renders templates with language selection, data validation, and
    GSM-7 / UCS-2 segment counting.

    TEACHING NOTE: The renderer is stateless beyond the settings cache.
    It's safe to instantiate once and reuse across requests / workers.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    def render(
        self,
        template_name: str,
        context: dict,
        language: str = "en",
        force_max_segments: Optional[int] = None,
    ) -> str:
        """
        Render a template with the given context in the chosen language.

        Args:
            template_name: key in TEMPLATES dict
            context: dict of placeholder values
            language: "en" (English) or "sw" (Swahili)
            force_max_segments: override default max segments

        Returns:
            Rendered message body

        Raises:
            ValueError: if template not found, language missing, or
                        rendered body exceeds max segments.

        TEACHING NOTE: If a template lacks the requested language, we
        fall back to English (the official language) and log a warning.
        This prevents a missing translation from blocking a critical
        payment confirmation.
        """
        template = TEMPLATES.get(template_name)
        if not template:
            raise ValueError(f"Template '{template_name}' not found")

        # Select language variant, fall back to English if missing.
        body_template = template.bodies.get(language)
        if not body_template:
            # Graceful fallback: English is always present.
            body_template = template.bodies.get("en")
            if not body_template:
                raise ValueError(
                    f"Template '{template_name}' has no 'en' or '{language}' variant"
                )

        # Render with graceful fallback for missing keys.
        try:
            body = body_template.format(**context)
        except KeyError:
            # Replace missing keys with empty string (no crash).
            keys = self._extract_keys(body_template)
            safe_context = {k: context.get(k, "") for k in keys}
            body = body_template.format(**safe_context)

        # Check segment count (cost control — each segment costs KES ~1).
        max_seg = force_max_segments or template.max_segments or self.settings.max_sms_segments
        segments = self._count_segments(body)
        if segments > max_seg:
            raise ValueError(
                f"Rendered message exceeds {max_seg} segments ({segments}). "
                f"Body: {body[:50]}..."
            )

        return body

    def _extract_keys(self, template_body: str) -> set[str]:
        """Extract {placeholder} names from a template string."""
        import re
        return set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template_body))

    def _count_segments(self, body: str) -> int:
        """
        Count SMS segments based on encoding.

        GSM-7 (standard SMS charset):
          - 1 segment = 160 chars
          - 2+ segments = 153 chars each (7 chars lost to UDH header)

        UCS-2 (Unicode / non-GSM-7):
          - 1 segment = 70 chars
          - 2+ segments = 67 chars each

        TEACHING NOTE: Swahili uses standard Latin letters (a-z, A-Z)
        which are all in the GSM-7 charset. So most SW templates count
        as GSM-7 segments. If a customer's name contains a non-GSM-7
        character (e.g., a Chinese name), the whole message flips to
        UCS-2 and segment capacity drops — this function detects that.
        """
        gsm7_chars = set(
            "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;"
            "<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
        )
        is_gsm7 = all(c in gsm7_chars for c in body)

        if is_gsm7:
            chars_per_seg = 160 if len(body) <= 160 else 153
        else:
            chars_per_seg = 70 if len(body) <= 70 else 67

        if len(body) <= chars_per_seg:
            return 1
        return (len(body) + chars_per_seg - 1) // chars_per_seg

    def list_templates(self) -> list[str]:
        """Return all available template names."""
        return list(TEMPLATES.keys())

    def list_languages(self, template_name: str) -> list[str]:
        """Return available languages for a template."""
        template = TEMPLATES.get(template_name)
        if not template:
            return []
        return list(template.bodies.keys())


# ── Convenience function ──

def render_template(
    template_name: str,
    context: dict,
    language: str = "en",
) -> str:
    """Quick render without creating a renderer instance."""
    renderer = TemplateRenderer()
    return renderer.render(template_name, context, language)