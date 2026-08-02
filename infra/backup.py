"""
infra/backup.py — Automated PostgreSQL backup utilities
════════════════════════════════════════════════════════════════════════

PURPOSE
-------
Encrypted database backups for disaster recovery and compliance with
Kenya's Data Protection Act 2019 (which requires reasonable safeguards
against data loss and unauthorised access).

Backup pipeline:
  1. ``pg_dump`` streams the database to a SQL dump in memory.
  2. The dump is encrypted at rest with **Fernet** (symmetric
     AES-128-CBC + HMAC-SHA256). Only encrypted ``.sql.enc`` files are
     written to disk — no plaintext dump ever touches the filesystem.
  3. ``cleanup_old_backups`` prunes old files on a daily/weekly
     retention schedule.

KEY DESIGN DECISIONS
--------------------
  1. **pg_dump via asyncio subprocess** — the event loop is not blocked
     during large dumps. We read stdout to capture the SQL without
     writing an intermediate plaintext file.
  2. **Fernet encryption** — Fernet guarantees that a message encrypted
     with a given key cannot be manipulated or read without the key. It
     uses AES-128-CBC for encryption and HMAC-SHA256 for authentication,
     providing both confidentiality and integrity.
  3. **Encryption key from Settings** — ``Settings.backup_encryption_key``
     is a ``SecretStr`` loaded from the environment. In production,
     rotate this key quarterly and store it in a secrets manager —
     never in the git repository.
  4. **Retention policy** — ``cleanup_old_backups`` keeps the *keep_daily*
     most recent daily backups and up to *keep_weekly* end-of-week
     (Sunday) backups. Older files are deleted and the count of deleted
     files is returned for monitoring/alerting.
  5. **Celery-friendly entry point** — ``run_backup_job`` pulls
     settings, calls ``create_backup``, then prunes old backups in one
     atomic job suitable for ``celery_app.conf.beat_schedule``.

ADAPTATION FROM THE TUITION AGENT
--------------------------------
  - Backup prefix changed from ``"sms_db_backup"`` to
    ``"sms_first_backup"`` to reflect the new project identity.
  - Default backup directory changed from
    ``/var/backups/sms-tuition-agent`` to ``/var/backups/sms-first-agent``.
  - ``Settings.backup_encryption_key`` field is unchanged (already
    defined in ``infra/settings.py`` as a ``SecretStr``).
  - ``create_backup``, ``cleanup_old_backups``, and ``run_backup_job``
    functions are otherwise unchanged in logic.

TEACHING NOTES
--------------
  - ``pg_dump`` is invoked via ``asyncio.create_subprocess_exec`` (not
    ``shell=True``) to avoid shell-injection risks when the database URL
    contains special characters.
  - The SQLAlchemy async driver suffix (``+asyncpg``) is stripped from
    the URL before passing to ``pg_dump``, which expects a plain
    ``postgresql://`` URL.
  - ``Fernet.generate_key()`` produces a 32-byte url-safe base64-encoded
    key. Store it as a hex string in the ``BACKUP_ENCRYPTION_KEY`` env
    var.
  - The retention policy uses file modification time (``st_mtime``), not
    the filename timestamp, to determine age. This is more robust
    against clock skew but means that touching a backup file (e.g., via
    ``rsync``) could affect retention.
  - On the Raspberry Pi, backups should be written to an external USB
    drive or NFS mount, not the SD card, to reduce wear and survive SD
    card failure.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - ``infra/settings.py`` — provides ``get_settings()`` for
    ``database_url`` and ``backup_encryption_key``.
  - ``celery_app.py`` (or equivalent) — schedules ``run_backup_job`` via
    Celery Beat (e.g., daily at 02:00 EAT).
  - ``infra/redis_pool.py`` — unrelated, but both modules share the
    same Settings instance via ``get_settings()`` (lru_cache ensures
    only one parse per process).

════════════════════════════════════════════════════════════════════════
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

# Fernet is the recommended symmetric encryption primitive in the
# cryptography library. It provides AES-128-CBC + HMAC-SHA256 with
# automatic IV generation and timestamp embedding.
from cryptography.fernet import Fernet

# get_settings() returns a cached Settings instance (lru_cache).
# database_url and backup_encryption_key are SecretStr fields.
from infra.settings import get_settings

logger = logging.getLogger(__name__)

# ── Backup filename prefix and extension ──────────────────────────────
# Files are named: sms_first_backup_20260802_143022.sql.enc
# The timestamp in the filename is for human readability; the actual
# retention logic uses file modification time.
_BACKUP_PREFIX = "sms_first_backup"
_BACKUP_SUFFIX = ".sql.enc"


async def create_backup(
    database_url: str,
    output_dir: str,
    encryption_key: str,
) -> str:
    """
    Dump the database and write an encrypted backup file.

    This is a three-step pipeline:
      1. Run ``pg_dump`` via an async subprocess, capturing stdout.
      2. Encrypt the dump bytes with Fernet.
      3. Write the encrypted bytes to a ``.sql.enc`` file.

    Args:
        database_url: SQLAlchemy-style PostgreSQL URL
            (e.g., ``postgresql+asyncpg://user:pass@host:5432/db``).
            The ``+asyncpg`` driver suffix is stripped for ``pg_dump``,
            which expects a plain ``postgresql://`` URL.
        output_dir: Directory where the ``.sql.enc`` file is written.
            Created if it does not exist (``parents=True, exist_ok=True``).
        encryption_key: Fernet-compatible key (32 url-safe base64 bytes).
            Generate with ``Fernet.generate_key()`` and store in the
            ``BACKUP_ENCRYPTION_KEY`` environment variable.

    Returns:
        The absolute path to the encrypted backup file.

    Raises:
        RuntimeError: If ``pg_dump`` exits with a non-zero status
            (e.g., database is down, authentication failure).
        ValueError: If *encryption_key* is not a valid Fernet key.
    """
    # ── Step 0: Ensure the output directory exists ──
    # mkdir(parents=True) creates intermediate directories if needed.
    # exist_ok=True means no error if the directory already exists.
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # ── Step 0b: Strip the SQLAlchemy async driver suffix ──
    # pg_dump is a C program that does not understand SQLAlchemy driver
    # suffixes. It expects a plain postgresql:// URL.
    # We strip both +asyncpg (used by FastAPI) and +psycopg2 (used by
    # some sync tools) to be safe.
    pg_url = database_url.replace("+asyncpg", "").replace("+psycopg2", "")

    # ── Step 0c: Build the backup filename ──
    # UTC timestamp in the filename (YYYYMMDD_HHMMSS) for sorting and
    # human readability. We use UTC (not Africa/Nairobi) for filenames
    # to avoid ambiguity with DST — though Nairobi has no DST, using
    # UTC is a good global convention for backup files.
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"{_BACKUP_PREFIX}_{timestamp}{_BACKUP_SUFFIX}"
    backup_path = out_path / backup_filename

    logger.info("Starting database backup → %s", backup_path)

    # ── Step 1: Run pg_dump via async subprocess ─────────────────────
    # create_subprocess_exec (not shell=True) avoids shell injection
    # when the database URL contains special characters.
    # stdout=PIPE captures the SQL dump in memory; stderr=PIPE
    # captures any error messages.
    process = await asyncio.create_subprocess_exec(
        "pg_dump",
        pg_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # communicate() waits for the process to finish and returns
    # (stdout_bytes, stderr_bytes).
    dump_bytes, stderr_bytes = await process.communicate()

    # Check the exit code — non-zero means pg_dump failed.
    if process.returncode != 0:
        stderr_msg = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        logger.error("pg_dump failed (exit %s): %s", process.returncode, stderr_msg)
        raise RuntimeError(f"pg_dump exited with status {process.returncode}: {stderr_msg}")

    # ── Step 2: Encrypt the dump ─────────────────────────────────────
    # Fernet(key) constructs a cipher object. The key must be 32
    # url-safe base64-encoded bytes.
    # We accept both str and bytes for the key (str is the common case
    # since it comes from SecretStr.get_secret_value()).
    try:
        fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
    except Exception as exc:
        # Invalid key format — raise a ValueError so callers can
        # distinguish this from a pg_dump failure (RuntimeError).
        raise ValueError(f"Invalid Fernet encryption key: {exc}") from exc

    # encrypt() returns the ciphertext as bytes. The ciphertext
    # includes the timestamp and IV, so the same plaintext produces
    # different ciphertext each time (which is desirable for security).
    encrypted = fernet.encrypt(dump_bytes)

    # ── Step 3: Write the encrypted file ─────────────────────────────
    # write_bytes() writes the entire ciphertext in one syscall.
    # The file on disk is never plaintext — the dump only existed in
    # memory (dump_bytes) and was encrypted before touching disk.
    backup_path.write_bytes(encrypted)
    logger.info("Backup complete: %s (%d bytes encrypted)", backup_path, len(encrypted))

    # Return the absolute (resolved) path for use by run_backup_job()
    # and monitoring/alerting.
    return str(backup_path.resolve())


def cleanup_old_backups(
    backup_dir: str,
    keep_daily: int = 7,
    keep_weekly: int = 4,
) -> int:
    """
    Delete old backup files beyond the retention policy.

    Retention logic:
      - Keep the *keep_daily* most recent backups (regardless of age).
      - Additionally keep up to *keep_weekly* end-of-week backups
        (Sunday) that are older than the daily window, so you have
        longer-term recovery points.

    Default retention: 7 daily + 4 weekly = 11 backups maximum.

    Args:
        backup_dir: Directory containing ``.sql.enc`` backup files.
        keep_daily: Number of most-recent daily backups to keep
            (default: 7 — one week of daily backups).
        keep_weekly: Number of weekly (Sunday) backups to keep beyond
            the daily window (default: 4 — one month of weekly backups).

    Returns:
        The number of backup files deleted. This is useful for
        monitoring: if the count is consistently high, backups may be
        running too frequently; if it's always 0, check that old
        backups are being created.
    """
    backup_path = Path(backup_dir)
    if not backup_path.is_dir():
        # Directory doesn't exist — nothing to clean. This can happen
        # on first run or if the backup volume was unmounted.
        logger.warning("Backup directory does not exist: %s", backup_dir)
        return 0

    # ── Gather all backup files, sorted by mtime (newest first) ──
    # glob() matches the backup prefix + suffix pattern.
    # sorted() with reverse=True puts newest files first.
    files = sorted(
        backup_path.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    if not files:
        return 0

    # ── Track which files to keep (using a set for O(1) lookup) ──
    kept: set[Path] = set()
    deleted_count = 0

    # ── Keep the most-recent daily backups ──
    # files[:keep_daily] gives the N newest files.
    for f in files[:keep_daily]:
        kept.add(f)

    # ── Keep up to keep_weekly Sunday backups beyond the daily window ──
    # We scan files[keep_daily:] (the older files) and keep the first
    # keep_weekly files whose modification time falls on a Sunday.
    # datetime.weekday() returns 0–6 (Mon–Sun); 6 == Sunday.
    weekly_kept = 0
    for f in files[keep_daily:]:
        if weekly_kept >= keep_weekly:
            break
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        # weekday() == 6 → Sunday
        if mtime.weekday() == 6:
            kept.add(f)
            weekly_kept += 1

    # ── Delete everything not retained ──
    # Any file not in the `kept` set is deleted.
    for f in files:
        if f not in kept:
            try:
                f.unlink()  # delete the file
                deleted_count += 1
                logger.info("Deleted old backup: %s", f.name)
            except OSError as exc:
                # Log the error but continue — one failed deletion
                # should not prevent cleanup of other files.
                logger.error("Failed to delete %s: %s", f, exc)

    return deleted_count


async def run_backup_job() -> dict:
    """
    Celery-friendly entry point that runs a full backup cycle.

    Pulls configuration from ``Settings``, creates a new encrypted
    backup, then prunes old backups per the retention policy. This is
    the function that Celery Beat calls on a schedule.

    Suitable for wrapping in a Celery task::

        @celery_app.task(name="infra.backup.run_backup_job")
        def run_backup_task():
            import asyncio
            from infra.backup import run_backup_job
            return asyncio.run(run_backup_job())

    Or calling from an async Celery worker (if using
    ``celery[asyncio]`` or ``gevent`` pool).

    The backup directory defaults to ``/var/backups/sms-first-agent``
    but can be overridden with the ``BACKUP_DIR`` environment variable
    or a volume mount (recommended on the Pi: mount an external USB
    drive at this path).

    Returns:
        A dict with:
          - ``backup_path``: path to the newly created backup file.
          - ``old_backups_deleted``: number of old backups pruned.
          - ``timestamp``: ISO 8601 timestamp of the job completion.

        This dict is useful for Celery result inspection and for
        monitoring/alerting (e.g., alert if ``old_backups_deleted`` is
        unexpectedly 0, or if ``backup_path`` is missing).
    """
    # ── Pull settings (cached via lru_cache) ──
    settings = get_settings()

    # Extract the database URL from the SecretStr.
    database_url = settings.database_url.get_secret_value()

    # ── Validate the encryption key is present ──
    # backup_encryption_key has a default of SecretStr("") — if the
    # env var is not set, we cannot create encrypted backups.
    encryption_key = settings.backup_encryption_key
    if encryption_key is None:
        raise RuntimeError(
            "BACKUP_ENCRYPTION_KEY is not set — cannot create encrypted backup"
        )
    encryption_key_str = encryption_key.get_secret_value()

    # ── Determine the backup directory ──
    # Default: /var/backups/sms-first-agent
    # Override with the BACKUP_DIR env var (e.g., for external USB drive).
    backup_dir = os.getenv("BACKUP_DIR", "/var/backups/sms-first-agent")

    # ── Create the backup ──
    backup_path = await create_backup(
        database_url=database_url,
        output_dir=backup_dir,
        encryption_key=encryption_key_str,
    )

    # ── Prune old backups per retention policy ──
    deleted = cleanup_old_backups(backup_dir)

    # ── Return a summary dict for Celery / monitoring ──
    result = {
        "backup_path": backup_path,
        "old_backups_deleted": deleted,
        "timestamp": datetime.utcnow().isoformat(),
    }
    logger.info("Backup job complete: %s", result)
    return result