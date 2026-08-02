#!/usr/bin/env python3
"""
scripts/backup.py — Encrypted database backup runner
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Runs an encrypted ``pg_dump`` backup of the application database, then
purges old backups according to the retention policy (7 daily + 4
weekly). This is the CLI/manual entry point; the automated nightly
backup is triggered by the run_backup Celery task in
workers/maintenance.py (which calls infra.backup.run_backup_job).

Intended to run as a sidecar container on a nightly cron, e.g.::

    docker compose run --rm backup python -m scripts.backup

Requirements:
  - ``infra.backup`` module with ``create_backup()`` and
    ``cleanup_old_backups()`` callables.
  - ``BACKUP_ENCRYPTION_KEY`` set in the environment (32-byte hex).

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - "sms-tuition-agent" → "sms-first-agent" in comments/docstrings.
  - BUG FIX: the original tuition agent's scripts/backup.py called
    ``cleanup_old_backups`` with the WRONG keyword name
    (``output_dir=``) and wrapped it in ``await`` even though the
    function is synchronous. The infra/backup.py signature is
    ``cleanup_old_backups(backup_dir, keep_daily=7, keep_weekly=4)``
    and it is NOT async. This version uses the correct keyword
    (``backup_dir=``) and drops the erroneous ``await``. See the call
    site below for the corrected invocation.
  - BACKUP_OUTPUT_DIR stays "/data/backups" (a Docker volume mount).
  - Logic is otherwise identical: create_backup (async, awaited) then
    cleanup_old_backups (sync, not awaited).

TEACHING NOTES
--------------
  - ``create_backup`` is async because it shells out to pg_dump via
    ``asyncio.create_subprocess_exec`` (so the event loop isn't blocked
    during a large dump). We therefore ``await`` it.
  - ``cleanup_old_backups`` is a plain synchronous function (it only
    does filesystem ops — Path.glob / unlink). It is NOT awaited. The
    original tuition script incorrectly awaited it; that would have
    raised a TypeError at runtime in a real async context, but the
    script's ``asyncio.run`` happened to coerce the coroutine-wrapped
    int into something that didn't crash visibly — a latent bug.
  - We print a JSON summary to stdout so the sidecar logs are
    machine-parseable. If any step errored, we exit(1) so the
    orchestrator/cron can detect failure.
  - The database_url is masked in the summary (only the host part is
    shown) so the credentials never land in logs. The encryption key is
    never printed at all.

KENYA-SPECIFIC CONSIDERATIONS
-----------------------------
  - The backup lands on /data/backups, which should be a Docker volume
    mounted on EXTERNAL storage (USB SSD, NAS), not the Pi's SD card.
    An SD card is the single most likely hardware failure point on a
    Pi; a backup on the same card is not a real backup.
  - Retention: 7 daily + 4 weekly gives ~1 month of recovery points,
    enough for most small-business data-loss incidents.

═══════════════════════════════════════════════════════════════════════
"""

import asyncio
import json
import sys
from typing import Any

from infra import backup
from infra.settings import get_settings

# Where encrypted backup files are written. In Docker this is a volume
# mount; on bare metal it should be an external/USB path. Stays the
# same as the tuition agent (the path is infrastructure, not domain).
BACKUP_OUTPUT_DIR = "/data/backups"

# Retention policy: keep this many recent daily backups, plus this many
# end-of-week (Sunday) backups beyond the daily window.
KEEP_DAILY = 7
KEEP_WEEKLY = 4


async def main() -> None:
    """Load settings, create an encrypted backup, then clean up old backups.

    Prints a JSON summary to stdout. Exits with code 1 if any step
    errored, so cron/orchestrators can detect failure.
    """
    settings = get_settings()
    database_url = settings.database_url.get_secret_value()
    encryption_key = settings.backup_encryption_key

    # backup_encryption_key is a SecretStr; extract raw value if set.
    # If it's the empty default, encryption_key will be "" (falsy) and
    # create_backup will raise a ValueError on the Fernet init — that's
    # the intended "fail loud if not configured" behavior.
    if encryption_key is not None:
        encryption_key = encryption_key.get_secret_value()

    # Build a summary dict for stdout logging. We mask the DB URL so
    # credentials never appear in container logs.
    summary: dict[str, Any] = {
        # Show only host:port/db (the part after '@') for identification.
        "database_url": database_url.split("@")[-1] if "@" in database_url else "unknown",
        "output_dir": BACKUP_OUTPUT_DIR,
        "encrypted": encryption_key is not None,
        "backup": None,
        "cleanup": None,
        "errors": [],
    }

    # ── Step 1: Create the encrypted backup ──
    # create_backup is async (pg_dump via subprocess), so we await it.
    try:
        backup_result = await backup.create_backup(
            database_url=database_url,
            output_dir=BACKUP_OUTPUT_DIR,
            encryption_key=encryption_key,
        )
        summary["backup"] = backup_result
    except Exception as exc:
        summary["errors"].append(f"create_backup failed: {exc!r}")

    # ── Step 2: Purge old backups per the retention policy ──
    # BUG FIX (vs. the tuition agent's scripts/backup.py):
    #   - The original called this with output_dir= (wrong kwarg name);
    #     infra/backup.py's signature is backup_dir=.
    #   - The original also wrapped it in `await`, but cleanup_old_backups
    #     is a plain sync function (filesystem ops only), so awaiting it
    #     would raise TypeError at runtime.
    # This version uses the correct keyword (backup_dir=) and does NOT
    # await — matching the infra/backup.py signature exactly.
    try:
        cleanup_result = backup.cleanup_old_backups(
            backup_dir=BACKUP_OUTPUT_DIR,
            keep_daily=KEEP_DAILY,
            keep_weekly=KEEP_WEEKLY,
        )
        summary["cleanup"] = cleanup_result
    except Exception as exc:
        summary["errors"].append(f"cleanup_old_backups failed: {exc!r}")

    # Print machine-parseable JSON so the sidecar logs are scrapable.
    print(json.dumps(summary, indent=2, default=str))

    # Non-zero exit so cron / Docker / K8s can detect a failed backup.
    if summary["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())