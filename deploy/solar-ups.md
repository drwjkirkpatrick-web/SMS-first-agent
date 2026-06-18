# Solar / UPS Power Resilience Guide for Rural Kenya

**For:** SMS-First Agent deployed in areas with unreliable grid power

---

## The Problem

Rural Kenya experiences frequent power outages — sometimes multiple times
per day, lasting minutes to hours. The SMS-First Agent must survive these
without data loss or duplicate SMS sends.

---

## Solution Architecture

```
┌────────────┐     ┌──────────┐     ┌──────────────┐     ┌───────────┐
│ Solar Panel│────▶│ Charge   │────▶│ Battery Bank │────▶│ Raspberry │
│  (20-50W)  │     │Controller│     │ (12V, 20-40Ah)│     │ Pi 4/5   │
└────────────┘     └──────────┘     └──────────────┘     └───────────┘
                                                            │
                                                    ┌───────┴───────┐
                                                    │ PostgreSQL    │
                                                    │ Redis         │
                                                    │ Celery        │
                                                    │ FastAPI       │
                                                    └───────────────┘
```

### Alternative: Simple UPS
If solar is not feasible, a basic UPS (1500VA, ~KES 8,000-15,000) provides
20-40 minutes of runtime during outages — enough for graceful shutdown.

---

## PostgreSQL Crash Safety

PostgreSQL's WAL (Write-Ahead Logging) guarantees that committed transactions
survive crashes. No special config needed — it's enabled by default.

### Recommended settings (postgresql.conf):
```ini
# Ensure durability (default, but explicit for clarity)
wal_level = replica
synchronous_commit = on
full_page_writes = on

# For Pi storage (reduce WAL size if SD card space is tight)
max_wal_size = 256MB
min_wal_size = 64MB

# Checkpoint tuning (reduce I/O spikes)
checkpoint_timeout = 5min
checkpoint_completion_target = 0.9
```

### After a crash recovery:
PostgreSQL automatically replays WAL on startup. The transactional outbox
guarantees:
- Committed messages are in the outbox (will be sent)
- Uncommitted messages are rolled back (never existed)
- No partial states (no "sending" without a corresponding message record)

---

## systemd Restart=Always

The Docker Compose file and systemd service both set `restart: always` /
`Restart=always`. This means:

1. **Power cut → Pi shuts down** (no graceful shutdown, but WAL protects data)
2. **Power returns → Pi boots** → systemd starts Docker → containers start
3. **PostgreSQL starts** → replays WAL → consistent state
4. **Celery workers start** → reconciliation loop finds any `SENDING` messages
   stuck from the crash → queries provider → resolves to `SENT` or `FAILED`
5. **SMS sends resume** — any PENDING messages from before the outage go out

---

## Battery Sizing

| Component | Power Draw | Daily Usage |
|-----------|-----------|------------|
| Raspberry Pi 4 | ~3W | 72 Wh |
| Pi + SSD | ~5W | 120 Wh |
| 4G USB modem | ~2W | 48 Wh |
| **Total** | **~7W** | **168 Wh** |

### Recommended battery:
- 12V, 20Ah lead-acid (240 Wh usable at 50% DoD) → ~1.4 days runtime
- 12V, 40Ah lead-acid (480 Wh usable) → ~2.8 days runtime
- 12V LiFePO4 20Ah (240 Wh, 80% DoD) → ~1.7 days, lasts 2000+ cycles

### Recommended solar panel:
- 30W panel → ~4 hours of full sun charges the 20Ah battery
- 50W panel → charges faster, handles cloudy days better

---

## Connectivity During Power Outage

If using a 4G USB modem for internet:
- The modem runs off the Pi's USB port (~2W)
- During a grid outage, the cell tower may also lose power
- SMS messages queue in the outbox as PENDING
- When connectivity returns, the connectivity watcher detects it
- Send workers resume and flush the backlog

---

## Data Loss Prevention Checklist

| Scenario | Protection |
|----------|-----------|
| Power cut mid-SMS-send | Message stuck in SENDING → reconciliation resolves |
| Power cut during DB write | PostgreSQL WAL replay → transaction rolls back |
| Power cut during outbox insert | Transaction not committed → nothing lost |
| Power cut after SMS sent, before DB commit | SENDING state → reconciliation queries provider |
| SD card corruption from sudden power loss | Use NVMe SSD (USB adapter) or UPS for graceful shutdown |
| Repeated power cycles wear SD card | Use SSD or USB boot instead of SD card boot |

---

## Recommended Hardware (Kenya, 2024 prices)

| Item | Specification | Est. Price (KES) |
|------|--------------|-----------------|
| Raspberry Pi 5 | 8GB RAM | 12,000-15,000 |
| NVMe SSD (128GB) | USB 3.0 enclosure | 3,000-5,000 |
| 4G USB modem | Huawei E8372 or similar | 3,000-5,000 |
| UPS (1500VA) | Basic APC equivalent | 8,000-15,000 |
| Solar panel 30W | With charge controller | 4,000-6,000 |
| Battery 12V 20Ah | Lead-acid or LiFePO4 | 3,000-8,000 |
| **Total** | | **33,000-54,000** |

---

## Monitoring Power Health

The connectivity watcher can be extended to monitor power status:

```bash
# Check if running on battery (if UPS provides this via USB)
upsc ups@localhost battery.charge
upsc ups@localhost ups.status
```

Add a Celery Beat task to check UPS status and send an alert SMS to the
business owner when battery is low.