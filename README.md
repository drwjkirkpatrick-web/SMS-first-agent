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

### v2 Improvements (Efficiency, Security, Resilience)

Ported from the SMS-Tuition-Agent v2 build — 30 improvements across 3 categories:

#### Efficiency (10)
- **E1** Bulk contact loading in reminder worker
- **E2** Accurate insert/duplicate counting via `RETURNING` clause
- **E3** TemplateRenderer used in send worker (no hardcoded strings)
- **E4** Delivery query by provider message ID (not body scan)
- **E5** Single `GROUP BY` query for dashboard stats
- **E6** Redis-cached business reminder policy (5-min TTL)
- **E7** Configurable DB connection pool sizing (`DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW`)
- **E8** Batch outbox polling with JOIN for contact phone
- **E9** Index optimization (partial + composite indexes via Alembic migration 002)
- **E10** Celery task time limits (300s hard, 240s soft)

#### Security (10)
- **S1** Redis-backed rate limiting on admin endpoints (60 req/min per IP)
- **S2** Admin token enforcement at startup in production
- **S3** Twilio webhook signature — full algorithm
- **S4** Input sanitization for inbound SMS bodies
- **S5** Dynamic `business_id` resolution (no hardcoded defaults)
- **S6** Configurable CORS allowed origins
- **S7** Transactional audit logging (optional session parameter)
- **S8** TLS enforcement via `docker-compose.prod.yml`
- **S9** Phone number validation on inbound
- **S10** PII masking in logs via `PIIMaskingFilter`

#### Resilience (10)
- **R1** Persistent event loop optimization for Celery tasks
- **R2** Worker health check script (`scripts/health_check.py`)
- **R3** Graceful shutdown (`task_reject_on_worker_lost`, `worker_hijack_root_logger`)
- **R4** Dead letter queue for poison messages (`domain/dead_letter.py`)
- **R5** Quiet hours enforcement in send worker (`domain/quiet_hours.py`)
- **R6** Data retention purge job (`domain/retention.py`, daily at 3 AM EAT)
- **R7** Circuit breaker for external API calls (`infra/circuit_breaker.py`)
- **R8** Reconciliation max-age cutoff (72h ceiling)
- **R9** Failure threshold alerting (`domain/alerting.py`, every 15 min)
- **R10** Automated encrypted database backup (`infra/backup.py`, daily at 2 AM EAT)

See [docs/30-improvements.md](docs/30-improvements.md) for the full specification.

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
├── docs/                   # 30-improvements spec, Kenya DPA compliance, deployment guides
├── design/                 # Architecture decisions (duplicate prevention, M-Pesa flow)
├── api/                    # FastAPI routers (webhooks, admin with rate limiting)
├── workers/                # Celery tasks (reminders, sends, reconciliation, campaigns, maintenance)
├── domain/                 # Business logic (models, services, templates, alerting, dead_letter, quiet_hours, retention)
├── adapters/               # External integrations (Africa's Talking, M-Pesa, Twilio)
├── infra/                  # Database, Redis, settings, audit, backup, circuit_breaker, rate_limiter, logging_filter
├── tests/                  # Unit (65) and integration tests
├── alembic/                # Database migrations (001 initial + 002 indexes + dead_letter)
├── deploy/                 # ARM64 deployment + solar/UPS power resilience
├── scripts/                # CSV import, promo CLI, health_check, backup
├── docker-compose.yml      # Development compose
├── docker-compose.prod.yml # Production compose with TLS + backup sidecar
└── .env.example            # All configuration variables
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