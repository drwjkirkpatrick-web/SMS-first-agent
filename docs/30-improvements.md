# 30 Improvements — Efficiency, Security, and Resilience

**Date:** 2026-08-02  
**Author:** Hermes Agent (glm-5.2)  
**Scope:** 30 testable improvement prompts for the SMS-First Agent

---

## Efficiency (10)

### E1. Bulk Contact Loading in Reminder Worker
**Prompt:** The reminder worker (`workers/reminders.py`) loads contacts one-by-one inside a loop (`select(Contact).where(Contact.id == candidate.contact_id)`). Refactor to bulk-load all contacts for the batch of candidates in a single query using `Contact.id.in_([list of IDs])`. Return a dict mapping `contact_id → Contact` for O(1) lookup. Test: verify the same candidates are produced with the bulk approach vs. the per-row approach.

### E2. Accurate Insert/Duplicate Counting in DispatchService
**Prompt:** `DispatchService.insert_outbox_messages()` always returns `{"inserted": len(values), "duplicates_skipped": 0}` — it can't distinguish inserts from conflicts. Use `session.execute(stmt)` and inspect `result.rowcount` or use `insert(...).on_conflict_do_nothing().returning(OutboundMessage.id)` to get the actual count of inserted rows. Compute `duplicates_skipped = len(values) - actual_inserted`. Test: insert 3 candidates, re-insert same 3, verify second call returns `inserted=0, duplicates_skipped=3`.

### E3. Use TemplateRenderer in Send Worker
**Prompt:** `workers/sends.py::_render_body()` has hardcoded template strings that duplicate `domain/templates.py`. Replace `_render_body` with a call to `TemplateRenderer.render()`, loading contact name, customer name, business name, amount, and due date from the DB. Test: verify a DUE_14 message renders with the actual business name and customer first name.

### E4. Africa's Talking Delivery Query by SID Instead of Body Scan
**Prompt:** `AfricasTalkingAdapter.query_delivery()` lists 100 recent messages and scans `msg.body` for `client_message_id` — O(N) and unreliable. Refactor to query by `provider_message_id` (the stored Africa's Talking message ID) using the API fetch-by-ID endpoint. Update the reconciliation worker to pass `message.provider_message_id` instead of `message.client_message_id`. The Twilio fallback adapter should follow the same pattern using `self.client.messages(sid).fetch()`. Test: mock the Africa's Talking fetch-by-ID call and verify the adapter returns the correct status.

### E5. Single GROUP BY Query for Dashboard Stats
**Prompt:** `api/admin.py::dashboard_stats()` runs a separate `COUNT(*)` query for each `MessageStatus` and `TransactionStatus` enum value — 15+ queries. Refactor to a single `SELECT status, COUNT(*) FROM outbound_messages WHERE business_id=:id GROUP BY status` and similarly for transactions. Test: verify the returned dict matches the per-status approach.

### E6. Cache Business Reminder Policy in Redis
**Prompt:** `PolicyService.load_policy()` hits the database every time the scheduler runs. Cache the policy in Redis with a 5-minute TTL, keyed by `business:{id}:policy`. Invalidate on `save_policy()`. Test: call `load_policy()` twice, verify the second call hits Redis (mock `redis_client.get`).

### E7. Worker DB Connection Pool Sizing
**Prompt:** `infra/database.py` uses `pool_size=5, max_overflow=0` for all services. Workers need more connections when scaled (e.g., 3 workers × 2 concurrency = 6 concurrent sessions). Add `DATABASE_POOL_SIZE` and `DATABASE_MAX_OVERFLOW` env vars with sensible defaults (5/10 for API, 10/20 for workers). Test: verify settings load custom pool sizes and the engine is created with them.

### E8. Batch Outbox Polling with JOIN for Contact Phone
**Prompt:** `workers/sends.py` loads contact phone per-message with a separate query. Refactor `OutboxService.poll_pending()` to JOIN contact data in the initial query (eager load `Contact.phone`). Test: verify the polled messages include contact phone numbers without a second query round-trip.

### E9. Index Optimization for Common Query Patterns
**Prompt:** Several high-frequency queries lack covering indexes: (1) `outbound_messages` WHERE `status='pending' AND retry_count < max_retries` — add a partial index on `(retry_count, max_retries)` WHERE `status='pending'`. (2) `transactions` WHERE `business_id=X AND status IN (pending, partial) AND due_date < Y` — add a composite index on `(business_id, status, due_date)`. (3) `inbound_messages` WHERE `business_id=X AND intent='call' AND processed_at IS NULL` — add partial index. Write a new Alembic migration. Test: migration applies cleanly and `EXPLAIN ANALYZE` shows index usage.

### E10. Celery Task Time Limits and Soft Limits
**Prompt:** No Celery task has a time limit. A hung Africa's Talking API call can block a worker indefinitely. Add `task_time_limit=300` (hard kill after 5 min) and `task_soft_time_limit=240` (soft timeout raises `SoftTimeLimitExceeded` after 4 min) to `celery_app.conf`. Add per-task overrides for long-running tasks. Test: verify a task that sleeps 10s with `task_time_limit=2` raises `TimeLimitExceeded`.

---

## Security (10)

### S1. Rate Limiting on Admin API Endpoints
**Prompt:** The admin API has no rate limiting — an attacker with a valid token can flood requests. Add a Redis-backed rate limiter middleware: 60 requests/minute per IP for admin endpoints. Return 429 with `Retry-After` header when exceeded. Test: make 61 requests in 1 second, verify the 61st returns 429.

### S2. Enforce Admin Token Configuration at Startup
**Prompt:** `Settings.admin_token_hash` defaults to `None`. If not set, admin endpoints return 501 but the app starts silently. Add a validation check in the FastAPI lifespan: if `app_env == "production"` and `admin_token_hash` is None, refuse to start. Test: set `app_env=production` without `admin_token_hash`, verify `RuntimeError` is raised.

### S3. Africa's Talking Webhook Signature — Full Algorithm
**Prompt:** `AfricasTalkingAdapter._compute_signature()` concatenates `url + body`, but the actual webhook signature algorithm requires computing an HMAC over the request payload with the Africa's Talking API key. Implement the correct algorithm per the Africa's Talking webhook documentation. The Twilio fallback adapter should implement Twilio's algorithm: parse the form body, sort by key, build the signature string as `url + key1 + value1 + key2 + value2...`. Test: use the documented test vectors to verify signature correctness for both adapters.

### S4. Input Sanitization for Inbound SMS Bodies
**Prompt:** Inbound SMS bodies are stored raw in the database and used in audit logs. A malicious SMS could contain SQL injection payloads or log injection (newlines, control characters). Sanitize on storage: strip control characters (except newline/tab), cap at 1600 chars, and escape for log output. Test: send a body with `\x00` and `\n\nFAKE LOG LINE`, verify stored body has no null bytes and log entry is single-line.

### S5. Remove Hardcoded business_id=1
**Prompt:** Multiple files hardcode `business_id=1`: `celery_app.py` beat schedule, `api/webhooks/africas_talking.py` inbound handler, `workers/inbound.py`. For multi-business support and to prevent data leakage between tenants, resolve `business_id` dynamically from the contact's business or the receiving short code. Test: create two businesses, send an inbound SMS from a contact in business 2, verify the message is associated with business 2.

### S6. CORS Configuration for Admin API
**Prompt:** The FastAPI app has no CORS middleware. If the dashboard is served from a different origin, browsers block requests. Add `CORSMiddleware` with configurable allowed origins (default: localhost only for dev, explicit list for prod). Test: send a preflight `OPTIONS` request with an `Origin` header, verify `Access-Control-Allow-Origin` is returned.

### S7. Transactional Audit Logging
**Prompt:** `infra/audit_logger.py::log_audit_event()` opens its own session and commits independently. If the calling transaction rolls back, the audit event is still committed — breaking causal consistency. Refactor to accept an optional `session` parameter: if provided, insert into that session (same transaction); if not, fall back to standalone. Test: insert an audit event in a session, rollback the session, verify the event is NOT in the database.

### S8. TLS Enforcement for API in Production
**Prompt:** `docker-compose.yml` exposes port 8000 on 0.0.0.0 without TLS. In production, SMS webhook signatures and admin tokens travel in plaintext. Add a production docker-compose override that mounts TLS certificates and runs uvicorn with `--ssl-keyfile` and `--ssl-certfile`. Add a startup check: if `app_env=production` and no TLS config, log a warning. Test: verify production compose file includes SSL flags.

### S9. Phone Number Validation on Inbound
**Prompt:** `Contact.phone` is stored as `String(20)` with no format validation beyond E.164 comments. Inbound webhooks accept any `From` value. Add validation: reject phone numbers that don't match `^\+?[1-9]\d{1,14}$` (E.164). Log rejected attempts as potential spoofing. Test: send a webhook with `From=invalid`, verify 400 response and audit log entry.

### S10. PII Masking in Error Messages and Tracebacks
**Prompt:** Exception handlers in workers and webhooks catch errors and append `str(exc)` to results/logs. If a SQLAlchemy error includes a phone number or name in the message, PII leaks to logs — a violation of the Kenya Data Protection Act (2019). Wrap all exception logging with `mask_phone()` and `mask_name()` on the error string. Add a structured logging filter that auto-masks known PII patterns. Test: trigger a DB error containing a phone number, verify the logged message has the number masked.

---

## Resilience (10)

### R1. Replace asyncio.run() with Persistent Event Loop in Celery Tasks
**Prompt:** Every Celery task uses `asyncio.run()` which creates and destroys an event loop per call — expensive and prevents connection pooling reuse. Create a shared async loop per worker process using `asgiref.sync.async_to_sync` or a module-level loop with `loop.run_until_complete()`. Test: verify two consecutive task calls reuse the same event loop (check `id(asyncio.get_event_loop())` is the same).

### R2. Worker Health Check Endpoint
**Prompt:** Celery workers have no health check — Docker can't detect a zombie worker that's alive but not processing tasks. Add a Celery `inspect`-based health check script that verifies workers respond to ping and have no long-running tasks. Integrate into docker-compose healthcheck. Test: start a worker, run the health check, verify it returns healthy; kill the worker, verify unhealthy.

### R3. Graceful Shutdown for Workers
**Prompt:** When Docker stops a worker, it sends SIGTERM. Celery's default behavior is to abandon in-progress tasks. Add `worker_hijack_root_logger=True`, `task_reject_on_worker_lost=True`, and a SIGTERM handler that finishes the current task before exiting. Set `worker_prefetch_multiplier=1` (already done) so only one task is in-flight. Test: send SIGTERM to a worker mid-task, verify the task completes and the worker exits cleanly.

### R4. Dead Letter Queue for Poison Messages
**Prompt:** If a message fails all retries, it stays in `FAILED` status with no further action. Add a dead letter queue: after `max_retries` exhausted, move the message to a `dead_letter_messages` table (or a Redis list) with the failure reason. Alert the business owner via the admin dashboard. Test: create a message that always fails, run the worker 4 times, verify it appears in the dead letter queue.

### R5. Quiet Hours Enforcement in Send Worker
**Prompt:** The policy service defines quiet hours, but `workers/sends.py` doesn't check them before sending. Add a quiet-hours check: before claiming a message, load the business's policy; if current time is in quiet hours, defer the message by setting `scheduled_at` to the next allowed time. Test: set quiet hours to encompass the current time, run the send worker, verify no messages are sent and `scheduled_at` is updated.

### R6. Data Retention Purge Job
**Prompt:** The security policy defines retention limits (sent SMS 90 days, failed 30 days, callbacks 30 days, inbound 30 days) but no job enforces them. Add a Celery Beat task that runs daily, deletes records past their retention period, and logs the purge count to audit. Test: insert a message with `created_at` 91 days ago, run the purge, verify it's deleted.

### R7. Circuit Breaker for Africa's Talking API
**Prompt:** If Africa's Talking is down, every send attempt fails, retries, and wastes resources. Implement a circuit breaker: after 5 consecutive failures, stop attempting sends for 60 seconds (OPEN state). After the cooldown, allow one test send (HALF_OPEN). If it succeeds, resume normal operation (CLOSED). Track state in Redis. Apply the same circuit breaker pattern to the Twilio fallback adapter with independent state tracking. Test: configure mock adapter to fail 5 times, verify the 6th send is short-circuited with a "circuit open" status.

### R8. Reconciliation Max-Age Cutoff
**Prompt:** `OutboxService.get_unknown_deliveries()` has no max-age limit — it will try to reconcile messages from months ago, wasting API calls. Add an `older_than_minutes` floor AND a `not_older_than_hours` ceiling (default 72 hours). Messages older than 72 hours are marked FAILED with reason `reconciliation_timeout`. Test: create a message with `updated_at` 73 hours ago, run reconciliation, verify it's marked FAILED.

### R9. Failure Threshold Alerting
**Prompt:** There's no alerting when failure rates spike. Add a periodic check (every 15 min): if the failure rate (FAILED / total sent) in the last hour exceeds 20%, log a critical audit event and send an SMS alert to the admin phone number. Test: create 10 messages, 3 failed, run the alert check, verify an alert is generated.

### R10. Automated Database Backup
**Prompt:** The pilot runbook describes manual `pg_dump` but no automated backup. Add a backup sidecar container in docker-compose that runs `pg_dump` nightly, encrypts with `BACKUP_ENCRYPTION_KEY`, and stores to a mounted volume. Retain 7 daily + 4 weekly backups. Test: run the backup container, verify a compressed encrypted backup file exists in the volume.