# SMS-First Agent for Kenyan Small Business Customer Engagement

> **Repository:** [github.com/drwjkirkpatrick-web/SMS-first-agent](https://github.com/drwjkirkpatrick-web/SMS-first-agent)
>
> **Status:** Planning / PRD Draft
>
> **Origin:** Adapted from [SMS-tuition-agent](https://github.com/drwjkirkpatrick-web/SMS-tuition-agent) — a school tuition reminder system built for ARM64 edge deployment.

---

## 1. Purpose

A headless, SMS-only backend that lets a **small business in Kenya** (retail shop, clinic, salon, hardware store, farm cooperative, etc.) automate **customer engagement** via SMS — reminders, promotions, loyalty, payment follow-up, and two-way customer interaction — without requiring customers to have a smartphone, internet, or email.

Built for **edge deployment** on Raspberry Pi 4/5 or similar ARM64 hardware, with **offline-resilient** operation for areas with unreliable electricity and internet.

---

## 2. Why Kenya?

Kenya's mobile landscape makes SMS-first the **optimal** customer channel:

| Factor | Detail |
|--------|--------|
| Mobile penetration | ~95% of adults have a mobile phone (feature phones dominate outside Nairobi) |
| SMS universality | Every phone receives SMS — no app download, no data plan needed |
| M-Pesa integration | Mobile money is the primary payment rail; SMS confirms transactions |
| Cost sensitivity | SMS costs ~KES 1 per message; data apps are unaffordable for many |
| Rural reality | Large rural population with 2G/3G coverage but limited 4G/LTE |
| Regulatory framework | Kenya ICT Act + Data Protection Act (2019) governs SMS marketing |
| Business culture | SMS is a respected business communication channel in Kenya |

---

## 3. Core Adaptations from Tuition Agent → Business Customer Engagement

The following table maps each **original capability** from the tuition agent to its **business customer engagement** equivalent, listing what changes, what stays, and what is new.

### 3.1 What We Keep As-Is (Proven Code)

| # | Original Capability | Why It Works for Kenya |
|---|---------------------|------------------------|
| 1 | **12-Layer Anti-Duplicate Algorithm** | Duplicate SMS destroys customer trust. The deterministic `message_key`, `ON CONFLICT DO NOTHING`, `FOR UPDATE SKIP LOCKED`, provider-side idempotency, and webhook dedup are **universal** — they apply equally to business promotions, appointment reminders, and payment follow-ups. **No changes needed.** |
| 2 | **Transactional Outbox Pattern** | The scheduler→outbox→worker pipeline is domain-agnostic. Any business can schedule messages and rely on atomic delivery. **No changes needed.** |
| 3 | **State Machine** (pending→sending→sent→delivered→failed→unknown) | Applies to all SMS delivery — business reminders, promos, receipts. **No changes needed.** |
| 4 | **Quiet Hours Enforcement** | Kenya businesses don't want SMS at 2 AM either. The policy engine with `start_hour`/`end_hour` works directly. Default quiet hours shift from 21:00–08:00 (US school) to business-configurable (e.g., 19:00–07:00 EAT). **Config-only change.** |
| 5 | **Audit Trail** (immutable, append-only) | Required for Kenya Data Protection Act compliance. **No changes needed.** |
| 6 | **Inbound Keyword Parser** (PAID, STATUS, CALL, HELP, STOP, START) | These keywords are universally useful. We add business-specific ones. **Extend, don't replace.** |
| 7 | **GSM-7 Segment Counting** | Critical for cost control — Kenyan SMS costs per segment. The `_count_segments()` function with GSM-7 / UCS-2 detection is **essential** for budget management. **No changes needed.** |
| 8 | **Retry Classification** (retryable vs non-retryable vs ambiguous) | Network failures in rural Kenya are more frequent than in the US — this classification is **even more important**. **No changes needed.** |
| 9 | **Reconciliation Loop** (unknown_delivery → query provider → resolve) | Rural network timeouts are common. The reconciliation worker that queries the provider for ambiguous deliveries is **critical** for Kenya. **No changes needed.** |
| 10 | **Multi-School / Multi-Tenant Safety** | Becomes multi-branch / multi-business safety. Same tenant isolation. **Rename, keep logic.** |
| 11 | **Policy Engine** (director-configurable timing, tone, cadence) | Becomes owner-configurable: promo frequency, reminder schedule, tone. **Rename fields, keep engine.** |
| 12 | **Soft Deletes** (`deleted_at` instead of physical DELETE) | Kenya Data Protection Act requires data retention controls. **No changes needed.** |

### 3.2 What We Adapt (Rename + Refocus)

| # | Original (Tuition) | Adapted (Business) | What Changes |
|---|---------------------|---------------------|--------------|
| A | School → Business | `School` model → `Business` model | Fields: `name`, `timezone` (Africa/Nairobi), `sms_opt_in_default` |
| B | Student → Customer | `Student` model → `Customer` model | `first_name`, `phone`, `email` (optional), `loyalty_points` |
| C | Guardian → Contact | `Guardian` model → `Contact` model | Phone number, opt-in/out, relationship (self, family, staff) |
| D | Invoice → Transaction | `Invoice` model → `Transaction` model | `amount`, `due_date` (optional), `type` (sale, credit, layaway, service) |
| E | Tuition Reminder → Appointment/Service Reminder | 14-day, 3-day, day-of reminders | Configurable per business type (clinic → appointment; salon → booking; hardware → layaway pickup) |
| F | Late Payment Notice → Credit Follow-Up | "Your payment of KES X is overdue" | Same logic, different template content |
| G | Payment Confirmation → M-Pesa Confirmation | "We received KES X via M-Pesa" | Integrate with M-Pesa webhook instead of SIS CSV |
| H | Hardship Extension → Credit Terms Request | "Request extended payment terms" | Same workflow, different terminology |
| I | Callback Request → Same | "Reply CALL for callback" | Same — universally useful |
| J | SIS Connector → CRM/POS Connector | CSV adapter → POS export, manual entry, API | Same factory pattern, new data sources |
| K | FERPA Awareness → Kenya DPA (2019) | Data minimization, masking, retention | Different regulation, same principles |

### 3.3 What Is New (Kenya-Specific Enhancements)

| # | Feature | Why It's Needed | Design Notes |
|---|---------|-----------------|--------------|
| i | **M-Pesa Integration** | M-Pesa is the dominant payment rail in Kenya (>70% of transactions). Customers expect M-Pesa payment confirmations and credit tracking via SMS. | New adapter: `MpesaAdapter` — webhook listener for Safaricom M-Pesa C2B confirmation, STK Push (Lipa na M-Pesa Online), and payment matching to customer accounts. Replaces Twilio as the *payment* integration (Twilio or Africa's Talking for SMS delivery). |
| ii | **Africa's Talking SMS Gateway** | Twilio is expensive for Kenyan SMS (international routing). Africa's Talking is a pan-African SMS API with local routing, lower per-SMS cost, and KES billing. | New adapter: `AfricasTalkingAdapter` implementing the same `SMSAdapter` interface. The adapter pattern means we can switch between Twilio and Africa's Talking via config — zero code changes in workers. |
| iii | **Swahili / English Bilingual Templates** | Kenya is bilingual: Swahili (national language) and English (official language). Customers may prefer either. SMS templates must support both. | `TemplateRenderer` extended with `language` parameter. Each template has `en` and `sw` variants. Customer record has `preferred_language` field. System auto-selects. |
| iv | **USSD Fallback** | Some customers (especially rural) are more comfortable with USSD (*123#) than SMS keyword replies. USSD sessions are interactive menus. | Phase 2: USSD adapter that translates inbound USSD menu selections into the same `InboundIntent` enum. The intent dispatcher doesn't care if input came from SMS keyword or USSD menu. |
| v | **Offline-First Operation** | Rural Kenya has frequent power and internet outages. The system must continue operating offline and sync when connectivity returns. | PostgreSQL local on the Pi (already in design). SMS gateway adapter gets a **local queue** — if Africa's Talking API is unreachable, messages are held in outbox with `status=PENDING` and retried when connectivity returns. The reconciliation loop handles this naturally. Add a **connectivity watcher** that pauses sends when offline and resumes when online. |
| vi | **Solar/UPS Power Resilience** | Frequent power cuts mean the Pi may shut down unexpectedly mid-send. | systemd service with `Restart=always`. PostgreSQL with WAL (Write-Ahead Logging) and crash recovery. The transactional outbox guarantees no partial sends. The reconciliation loop recovers any `SENDING` messages stuck from a crash. |
| vii | **Airtime / Data Cost Awareness** | Business owners are cost-sensitive. Each SMS costs money. The system must track and report SMS costs. | `OutboundMessage.price` field (already exists in model). Extend to track cost in KES. Dashboard shows daily/weekly/monthly SMS spend. Configurable budget cap: system warns or pauses when daily spend exceeds limit. |
| viii | **Customer Segmentation** | Business needs to target SMS to specific customer groups (e.g., "all customers who haven't visited in 30 days", "all customers in Eastlands"). | New `CustomerSegment` model: tags, last-visit date, location, purchase history. Campaign builder: select segment → compose message → schedule → dedupe via existing outbox. |
| ix | **Promotional Campaign Engine** | Schools don't send promos; businesses do. "Mama mboga" wants to SMS all customers about today's fresh produce. | New `Campaign` model with start/end, template, segment, frequency cap (e.g., max 1 promo per customer per week). Integrates with the dedupe system: promo `message_key` includes campaign ID + customer ID + date, preventing duplicate promos. |
| x | **Loyalty Points Tracking** | Kenyan businesses (especially retail) use loyalty programs. SMS can deliver points updates. | `Customer.loyalty_points` field. SMS: "You have 500 points! Redeem for KES 50 off your next purchase." |
| xi | **Layaway / Credit Tracking** | Many Kenyan businesses offer informal credit (kuku kienyeji farmer, hardware store, etc.). Customers pay in installments. | `Transaction.type = CREDIT`. Reminder engine sends balance reminders: "Your outstanding balance is KES 2,500. Last payment KES 500 on Jan 15." |
| xii | **M-Pesa STK Push Integration** | Instead of asking customers to send money manually, the business can trigger an STK Push that prompts the customer's phone to enter their M-Pesa PIN. | `MpesaAdapter.send_stk_push(phone, amount, account_ref)` → customer sees "Enter M-Pesa PIN" prompt. Result webhook confirms payment. |
| xiii | **Opt-Out Compliance (Kenya DPA 2019)** | Kenya Data Protection Act requires explicit consent and easy opt-out. STOP/START keywords already exist. Add: | Compliance log: every opt-out recorded with timestamp, source, and customer ID. Retention policy: opted-out customer data purged after 6 months (configurable). |
| xiv | **Business Hours Awareness** | Kenyan businesses have specific operating hours. SMS should respect them. | Policy engine: `business_hours_start` / `business_hours_end` in addition to quiet hours. Messages scheduled outside business hours are deferred to next business day. |
| xv | **Flash / Binary SMS Support** | For urgent payment reminders, flash SMS (appears immediately on screen, not stored in inbox) can be used. | Phase 2: adapter supports `flash=True` parameter. Use sparingly — only for overdue credit > 30 days. |
| xvi | **WhatsApp Bridge** | Many Kenyan businesses use WhatsApp Business for customer communication. SMS is the fallback; WhatsApp is preferred when customer has it. | Phase 3: WhatsApp Business API adapter. System checks if customer has WhatsApp; routes there first, SMS as fallback. Same dedupe, same templates, same outbox. |
| xvii | **Local Time / EAT Timezone** | All scheduling must use Africa/Nairobi (EAT, UTC+3). No DST in Kenya. | `School.timezone` default changes to `Africa/Nairobi`. No DST handling needed (simplifies code). |
| xviii | **Currency: KES** | All amounts in Kenya Shillings. No multi-currency needed for domestic. | Templates use `KES` prefix. `Numeric(10,2)` works fine. |
| xix | **Network-Aware Send Throttling** | Safaricom, Airtel, Telkom have different SMS routing. Some networks have delays. | Adapter can tag messages with recipient network (derived from phone number prefix). Dashboard shows delivery rate per network. |
| xx | **CSV Import for Business Onboarding** | Most small businesses track customers in a notebook or Excel. Easy CSV import is critical for adoption. | `CSVConnector` already exists — extend with customer/transaction import templates. Provide Excel template downloads. |

---

## 4. Architecture Overview

```
                    ┌──────────────────────────────────────────────────┐
                    │          SMS-First Agent (ARM64)                │
                    │          (Raspberry Pi 4/5)                      │
                    │                                                  │
  ┌──────────┐     │  ┌─────────┐  ┌──────────┐  ┌──────────────┐   │
  │  POS /   │────▶│  │ FastAPI  │  │ Celery   │  │ PostgreSQL   │   │
  │  Excel   │     │  │ Webhooks │  │ Workers   │  │ (local)      │   │
  │  CSV     │     │  │ Admin    │  │ Scheduler │  │ Redis        │   │
  └──────────┘     │  └────┬────┘  └────┬─────┘  └──────┬───────┘   │
                    │       │            │                │          │
                    │       ▼            ▼                ▼          │
                    │  ┌──────────────────────────────────────┐      │
                    │  │     Transactional Outbox              │      │
                    │  │  (12-Layer Anti-Duplicate Defense)   │      │
                    │  └────────────────┬─────────────────────┘      │
                    │                   │                            │
                    │       ┌───────────┼───────────────┐            │
                    │       ▼           ▼               ▼            │
                    │  Africa's    M-Pesa STK      Twilio (opt)       │
                    │  Talking     Push           (intl fallback)    │
                    │  (SMS)       (Payments)                       │
                    │                                                │
                    └──────────────────────────────────────────────────┘
                                       │
                                       ▼
                              ┌─────────────────┐
                              │  Customer Phone  │
                              │  (Feature phone  │
                              │   or smartphone) │
                              └─────────────────┘
```

---

## 5. Technical Stack (Inherited + Adapted)

| Component | Tuition Agent | Business Agent | Change |
|-----------|--------------|----------------|--------|
| Web Framework | FastAPI | FastAPI | Same |
| Task Queue | Celery + Redis | Celery + Redis | Same |
| Database | PostgreSQL 16 | PostgreSQL 16 | Same (local on Pi) |
| SMS Provider | Twilio | Africa's Talking (primary) + Twilio (fallback) | New adapter |
| Payments | SIS CSV | M-Pesa C2B + STK Push | New adapter |
| Scheduler | Celery Beat | Celery Beat | Same |
| Deployment | Docker Compose | Docker Compose | Same |
| Platform | ARM64 (Pi/Jetson) | ARM64 (Pi/Jetson) | Same |
| Timezone | America/Los_Angeles | Africa/Nairobi | Config change |
| Currency | USD | KES | Template change |
| Language | English | English + Swahili | New template layer |
| Compliance | FERPA | Kenya DPA 2019 | Policy update |

---

## 6. Project Structure (Planned)

```
SMS-first-agent/
├── prd/                    # Product requirements (this document)
├── docs/                   # Security policy, deployment guides, Kenya DPA compliance
│   ├── kenya-dpa-compliance.md
│   ├── africa-talking-setup.md
│   ├── mpesa-integration.md
│   └── deployment-kenya.md
├── design/                  # Architecture decisions
│   ├── duplicate-prevention.md    # Inherited from tuition agent
│   ├── mpesa-payment-flow.md
│   └── offline-operation.md
├── api/                     # FastAPI routers
│   ├── webhooks/
│   │   ├── africas_talking.py   # Inbound SMS webhook
│   │   ├── mpesa.py            # M-Pesa C2B confirmation
│   │   └── mpesa_stk.py        # STK Push callback
│   └── admin.py               # Dashboard, campaign management
├── workers/                 # Celery tasks
│   ├── reminders.py           # Appointment/service reminders
│   ├── sends.py               # Outbox polling + SMS dispatch
│   ├── reconciliation.py      # Unknown delivery resolution
│   ├── inbound.py             # Keyword parser + intent dispatch
│   ├── campaigns.py           # Promotional campaign engine
│   └── mpesa_reconciliation.py # M-Pesa payment matching
├── domain/                  # Business logic
│   ├── models.py             # Business, Customer, Contact, Transaction, Campaign
│   ├── reminder_service.py   # Reminder eligibility + message keys
│   ├── campaign_service.py   # Promo campaign builder + segment targeting
│   ├── templates.py          # Bilingual (EN/SW) template library
│   ├── outbox.py             # Transactional outbox (inherited)
│   ├── dispatch_service.py   # Outbox insertion (inherited)
│   ├── policy_service.py     # Business-configurable policy
│   ├── mpesa_service.py      # M-Pesa payment matching + STK Push
│   └── masking.py            # PII masking (inherited)
├── adapters/                # External integrations
│   ├── sms_adapter.py        # Abstract SMS interface (inherited)
│   ├── africas_talking.py    # Africa's Talking SMS adapter
│   ├── twilio_adapter.py     # Twilio fallback adapter (inherited)
│   ├── mock_adapter.py       # Test adapter (inherited)
│   ├── mpesa_adapter.py      # M-Pesa STK Push + C2B adapter
│   ├── csv_connector.py      # CSV import (inherited, extended)
│   ├── pos_connector.py      # POS integration stub
│   └── connector_factory.py  # Factory pattern (inherited)
├── infra/                   # Infrastructure
│   ├── database.py           # SQLAlchemy async engine (inherited)
│   ├── redis_pool.py         # Redis connection (inherited)
│   ├── settings.py           # Pydantic settings (Africa/Nairobi default)
│   ├── audit_logger.py       # Immutable audit log (inherited)
│   └── connectivity_watcher.py # Offline detection + send pause
├── tests/                   # Unit + integration tests
├── alembic/                 # Database migrations
├── deploy/                  # Deployment configs
│   ├── arm64-setup.md        # Raspberry Pi setup (inherited)
│   ├── solar-ups.md          # Power resilience guide
│   └── docker-compose.yml    # Updated services
└── scripts/                 # Utility scripts
    ├── import_customers.py   # CSV customer import
    └── send_promo.py         # Quick promo send CLI
```

---

## 7. Inbound SMS Keywords (Extended)

| Keyword | Tuition Agent | Business Agent | Action |
|---------|--------------|----------------|--------|
| `PAID` | Payment claim | Payment claim (M-Pesa ref) | Queue reconciliation |
| `STATUS` | Tuition balance | Account/credit balance | Reply with balance |
| `CALL` | Callback request | Callback request | Queue for staff |
| `HELP` | Command list | Command list | Send help (bilingual) |
| `STOP` | Opt out | Opt out (DPA 2019) | Suppress all SMS |
| `START` | Opt back in | Opt back in | Re-enable SMS |
| `PROMO` | — | Request current promotions | Send active promo |
| `POINTS` | — | Loyalty points balance | Send points total |
| `BALANCE` | — | Credit/layaway balance | Same as STATUS |
| `ORDER` | — | Order status (retail) | Reply with order status |
| `BOOK` | — | Book appointment (clinic/salon) | Create appointment |
| `HOURS` | — | Business hours | Reply with hours |
| `LOCATION` | — | Business location | Reply with address |

---

## 8. Kenya-Specific Compliance

### 8.1 Kenya Data Protection Act (2019)

| Requirement | Implementation |
|------------|----------------|
| Explicit consent for SMS marketing | `Contact.sms_opt_in` with timestamp + source |
| Easy opt-out (STOP keyword) | Already implemented; add 24-hour compliance window |
| Data retention limits | Configurable: delete opted-out customer data after 6 months |
| Data minimization | Store only: name, phone, transaction history, opt-in status |
| Right to access | Admin endpoint: export customer's data via SMS |
| Right to erasure | Admin endpoint: soft-delete → purge after retention period |
| Breach notification | Audit log + alert system for unauthorized access |
| No marketing to non-consenting numbers | Suppression layer checks opt-in before any send |

### 8.2 SMS Marketing Guidelines (Communications Authority of Kenya)

| Guideline | Implementation |
|-----------|----------------|
| Sender ID registration | Africa's Talking supports alphanumeric sender IDs (e.g., `MAMA-MBOGA`) |
| No SMS between 8 PM and 7 AM (marketing) | Quiet hours: `start_hour=20`, `end_hour=7` for promo type |
| Transactional SMS exempt from time limit | Reminder type has separate quiet hours config |
| Include opt-out instructions in marketing | Templates append "Reply STOP to opt out" |
| Maximum 3 marketing SMS per week per customer | Campaign frequency cap in policy engine |

---

## 9. Offline Operation Design

### 9.1 Connectivity Watcher

```python
# infra/connectivity_watcher.py (conceptual)
class ConnectivityWatcher:
    """
    Monitors internet connectivity by pinging Africa's Talking API
    endpoint every 30 seconds.

    When offline:
    - Pauses send workers (messages stay PENDING in outbox)
    - Continues accepting inbound SMS via webhook (if SIM card
      is on the Pi via USB modem — Phase 2)
    - Logs connectivity events for audit

    When online:
    - Resumes send workers
    - Flushes backlog (outbox poll picks up all PENDING)
    - Runs reconciliation for any UNKNOWN_DELIVERY
    """
```

### 9.2 Power Failure Recovery

| Scenario | What Happens | Recovery |
|----------|-------------|----------|
| Pi loses power mid-send | Message in SENDING state; no SMS was sent | On reboot, reconciliation finds SENDING → queries provider → resolves |
| Pi loses power during DB write | PostgreSQL WAL replay | Transaction rolls back; no orphaned messages |
| Pi loses power during outbox insert | Transaction not committed | Nothing inserted; scheduler re-runs next cycle |
| Pi loses power after SMS sent, before DB commit | Message in SENDING; provider has it | Reconciliation queries provider → marks SENT |

---

## 10. M-Pesa Integration Flow

### 10.1 STK Push (Customer Pays via Phone Prompt)

```
Business triggers STK Push
    │
    ▼
Safaricom sends "Enter M-Pesa PIN" to customer phone
    │
    ▼
Customer enters PIN → M-Pesa deducts → Safaricom sends webhook
    │
    ▼
SMS-first-agent receives webhook → matches to customer + transaction
    │
    ▼
Updates transaction balance → sends SMS confirmation
    │
    ▼
"Hi {name}, we received KES {amount} via M-Pesa. Balance: KES {balance}."
```

### 10.2 C2B (Customer Sends Money Manually)

```
Customer sends money via M-Pesa to business Paybill/Till number
    │
    ▼
Safaricom sends C2B confirmation webhook to SMS-first-agent
    │
    ▼
Agent matches by phone number + amount → records payment
    │
    ▼
Sends SMS: "Hi {name}, payment of KES {amount} received. Ref: {mpesa_code}."
```

---

## 11. Development Roadmap

### Phase 1: Core SMS Engine (Weeks 1–3)

Inherit and adapt the proven tuition agent core:

- [ ] Fork domain models: School→Business, Student→Customer, Guardian→Contact, Invoice→Transaction
- [ ] Adapt templates: KES currency, Africa/Nairobi timezone, English templates
- [ ] Port 12-layer duplicate prevention (no changes — universal)
- [ ] Port transactional outbox (no changes)
- [ ] Port state machine (no changes)
- [ ] Port reconciliation loop (no changes)
- [ ] Port audit trail (no changes)
- [ ] Implement `AfricasTalkingAdapter` (new SMS adapter, same interface)
- [ ] Configure for offline-first: local PostgreSQL, Redis, Celery all on Pi
- [ ] Deploy via Docker Compose on Raspberry Pi

### Phase 2: M-Pesa + Bilingual (Weeks 4–6)

- [ ] Implement `MpesaAdapter` (STK Push + C2B webhook)
- [ ] Add Swahili template variants
- [ ] Add `preferred_language` to customer model
- [ ] Implement connectivity watcher (offline detection)
- [ ] Add business hours to policy engine
- [ ] Add SMS cost tracking (KES per segment)
- [ ] Add daily SMS budget cap enforcement

### Phase 3: Campaigns + Segmentation (Weeks 7–9)

- [ ] Implement `CustomerSegment` model (tags, location, purchase history)
- [ ] Implement `Campaign` model (promo, frequency cap, schedule)
- [ ] Campaign builder admin endpoint
- [ ] Extended inbound keywords (PROMO, POINTS, BOOK, HOURS, LOCATION)
- [ ] Loyalty points tracking
- [ ] Layaway/credit balance reminders

### Phase 4: Compliance + Polish (Weeks 10–12)

- [ ] Kenya DPA compliance: retention policies, data export, erasure
- [ ] SMS marketing guidelines enforcement (time limits, frequency caps)
- [ ] Dashboard: delivery rates, cost tracking, campaign analytics
- [ ] CSV import tool with Excel template
- [ ] Pilot runbook for Kenyan small business deployment
- [ ] Documentation: Kenya-specific setup guide

### Phase 5: Advanced (Post-MVP)

- [ ] USSD fallback adapter
- [ ] WhatsApp Business bridge
- [ ] Multi-branch support
- [ ] Airtime top-up rewards (Safaricom B2B API)
- [ ] Voice IVR for customers who can't read SMS

---

## 12. License

MIT — see [LICENSE](LICENSE)

---

## Appendix A: Original Tuition Agent Feature Mapping

| Tuition Agent File | Business Agent File | Status |
|---------------------|---------------------|--------|
| `domain/models.py` (School, Student, Guardian, Invoice) | `domain/models.py` (Business, Customer, Contact, Transaction) | Adapted |
| `domain/reminder_service.py` | `domain/reminder_service.py` | Adapted (business types) |
| `domain/templates.py` | `domain/templates.py` | Adapted (KES, bilingual) |
| `domain/outbox.py` | `domain/outbox.py` | **Inherited (no changes)** |
| `domain/dispatch_service.py` | `domain/dispatch_service.py` | **Inherited (no changes)** |
| `domain/policy_service.py` | `domain/policy_service.py` | Adapted (business hours) |
| `domain/masking.py` | `domain/masking.py` | **Inherited (no changes)** |
| `domain/reconciliation_service.py` | `domain/reconciliation_service.py` | **Inherited (no changes)** |
| `domain/hardship_service.py` | `domain/credit_terms_service.py` | Renamed |
| `domain/invoice_service.py` | `domain/transaction_service.py` | Renamed |
| `adapters/sms_adapter.py` | `adapters/sms_adapter.py` | **Inherited (no changes)** |
| `adapters/twilio_adapter.py` | `adapters/twilio_adapter.py` | **Inherited (fallback)** |
| `adapters/mock_adapter.py` | `adapters/mock_adapter.py` | **Inherited (no changes)** |
| `adapters/csv_connector.py` | `adapters/csv_connector.py` | Extended (customer import) |
| `adapters/connector_factory.py` | `adapters/connector_factory.py` | **Inherited (no changes)** |
| `workers/reminders.py` | `workers/reminders.py` | Adapted |
| `workers/sends.py` | `workers/sends.py` | **Inherited (new adapter)** |
| `workers/reconciliation.py` | `workers/reconciliation.py` | **Inherited (no changes)** |
| `workers/inbound.py` | `workers/inbound.py` | Extended (new keywords) |
| `infra/database.py` | `infra/database.py` | **Inherited (no changes)** |
| `infra/redis_pool.py` | `infra/redis_pool.py` | **Inherited (no changes)** |
| `infra/settings.py` | `infra/settings.py` | Adapted (timezone, providers) |
| `infra/audit_logger.py` | `infra/audit_logger.py` | **Inherited (no changes)** |
| `design/duplicate-prevention.md` | `design/duplicate-prevention.md` | **Inherited (no changes)** |
| `deploy/arm64-setup.md` | `deploy/arm64-setup.md` | Extended (solar/UPS) |
| — | `adapters/africas_talking.py` | **New** |
| — | `adapters/mpesa_adapter.py` | **New** |
| — | `domain/campaign_service.py` | **New** |
| — | `infra/connectivity_watcher.py` | **New** |
| — | `docs/kenya-dpa-compliance.md` | **New** |
| — | `docs/mpesa-integration.md` | **New** |
| — | `docs/deployment-kenya.md` | **New** |