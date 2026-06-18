# SMS-First Agent

> SMS-only customer engagement platform for small businesses in Kenya. Built for Raspberry Pi, offline-first, M-Pesa-ready.

**Repository:** [github.com/drwjkirkpatrick-web/SMS-first-agent](https://github.com/drwjkirkpatrick-web/SMS-first-agent)

---

## What It Does

A headless backend that lets a Kenyan small business automate customer communication via SMS — appointment reminders, promotional campaigns, credit/layaway follow-up, M-Pesa payment confirmation, and two-way customer interaction — without requiring customers to have a smartphone, internet, or email.

**Customers** use any mobile phone (feature phone or smartphone). They text keywords like `STATUS`, `PAID`, `CALL`, `PROMO`, `BOOK`, `POINTS`, `STOP`.

**Business owners** manage via a simple admin dashboard or CSV import. No technical knowledge required.

---

## Why SMS-First for Kenya

| Factor | Detail |
|--------|--------|
| Mobile penetration | ~95% of adults have a mobile phone |
| SMS universality | Every phone receives SMS — no app, no data plan |
| M-Pesa | Mobile money is the primary payment rail |
| Cost | SMS at ~KES 1/message is affordable; data apps are not |
| Rural reality | 2G/3G coverage widespread; 4G/LTE limited outside cities |
| Regulation | Kenya Data Protection Act (2019) governs SMS marketing |

---

## Architecture

```
POS/Excel/CSV → FastAPI API → PostgreSQL → Africa's Talking → Customer Phone
                                Redis → Celery Workers (scheduler, sends, reconciliation)
                                        M-Pesa Webhook → Payment matching → SMS confirmation
```

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Web Framework | FastAPI | REST API, webhooks, admin dashboard |
| Task Queue | Celery + Redis | Background jobs (reminders, sends, reconciliation) |
| Database | PostgreSQL 16 | State, outbox, audit log (local on Pi) |
| SMS | Africa's Talking | Primary SMS gateway (local routing, KES billing) |
| SMS Fallback | Twilio | International routing backup |
| Payments | M-Pesa STK Push / C2B | Payment collection + confirmation |
| Scheduler | Celery Beat | Daily reminder + campaign scheduling |
| Deployment | Docker Compose | ARM64-ready containers |

---

## Key Features

### Inherited from SMS-Tuition-Agent (Battle-Tested)

- **12-Layer Anti-Duplicate Algorithm** — deterministic message keys, DB-level `ON CONFLICT DO NOTHING`, `FOR UPDATE SKIP LOCKED`, provider-side idempotency, webhook dedup, state machine, business-logic suppression
- **Transactional Outbox** — every send decision persisted atomically; no orphaned or duplicate messages
- **Reconciliation Loop** — handles network timeouts (critical for rural Kenya)
- **Audit Trail** — immutable, append-only (Kenya DPA compliant)
- **Retry Classification** — retryable vs non-retryable vs ambiguous error handling
- **GSM-7 Segment Counting** — cost control via accurate segment estimation
- **Quiet Hours** — no SMS during configured hours
- **Soft Deletes** — data retention controls

### New for Kenyan Small Business

- **Africa's Talking SMS Adapter** — local SMS routing, KES billing, lower cost
- **M-Pesa Integration** — STK Push (customer pays via phone prompt) + C2B webhook matching
- **Bilingual Templates** — English + Swahili, auto-selected per customer preference
- **Offline-First Operation** — connectivity watcher, local PostgreSQL, outbox holds messages during outages
- **Power Resilience** — systemd `Restart=always`, PostgreSQL WAL crash recovery, reconciliation recovers stuck sends
- **Promotional Campaign Engine** — segment targeting, frequency caps, scheduled promos
- **Loyalty Points Tracking** — SMS points updates
- **Layaway / Credit Reminders** — installment balance tracking
- **Customer Segmentation** — tag-based, location, purchase history, last-visit
- **SMS Cost Tracking** — KES per segment, daily budget caps
- **Business Hours Awareness** — deferred to next business day
- **Kenya DPA (2019) Compliance** — consent, opt-out, retention, erasure, data export

---

## Quick Start

```bash
git clone https://github.com/drwjkirkpatrick-web/SMS-first-agent.git
cd SMS-first-agent
cp .env.example .env  # fill in Africa's Talking + M-Pesa credentials
docker compose up --build -d
```

Verify: `curl http://localhost:8000/health`

---

## Inbound SMS Keywords

| Keyword | Action |
|---------|--------|
| `STATUS` | Reply with account/credit balance |
| `PAID` | Acknowledge payment (ask for M-Pesa ref) |
| `CALL` | Request callback from staff |
| `PROMO` | Send current promotions |
| `POINTS` | Loyalty points balance |
| `BOOK` | Book appointment (clinic/salon) |
| `HOURS` | Business hours |
| `LOCATION` | Business address |
| `HELP` | List all commands |
| `STOP` | Opt out of SMS |
| `START` | Opt back in |

---

## Project Structure

```
SMS-first-agent/
├── prd/                    # Product requirements
├── docs/                   # Kenya DPA compliance, deployment guides
├── design/                 # Architecture decisions (duplicate prevention, M-Pesa flow)
├── api/                    # FastAPI routers (webhooks, admin)
├── workers/                # Celery tasks (reminders, sends, reconciliation, campaigns)
├── domain/                 # Business logic (models, services, templates)
├── adapters/               # External integrations (Africa's Talking, M-Pesa, Twilio)
├── infra/                  # Database, Redis, settings, audit, connectivity watcher
├── tests/                  # Unit and integration tests
├── alembic/                # Database migrations
├── deploy/                 # ARM64 deployment + solar/UPS power resilience
└── scripts/                # CSV import, promo CLI
```

---

## Development Roadmap

| Phase | Weeks | Focus |
|-------|-------|-------|
| 1 | 1–3 | Core SMS engine (inherit from tuition agent, Africa's Talking adapter) |
| 2 | 4–6 | M-Pesa integration, bilingual templates, offline-first, cost tracking |
| 3 | 7–9 | Campaign engine, segmentation, loyalty, extended keywords |
| 4 | 10–12 | Kenya DPA compliance, dashboard, pilot runbook, documentation |
| 5 | Post-MVP | USSD fallback, WhatsApp bridge, multi-branch, voice IVR |

See [prd/PRODUCT_REQUIREMENTS.md](prd/PRODUCT_REQUIREMENTS.md) for the full specification.

---

## License

MIT — see [LICENSE](LICENSE)