"""
infra/settings.py - Typed application configuration (Kenya adaptation)
======================================================================

PURPOSE
-------
Pydantic Settings reads environment variables and validates them at
startup. If a required secret is missing or malformed, the app refuses
to start with a clear error message.

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - school_timezone -> business_timezone, default Africa/Nairobi
  - NEW: Africa's Talking SMS gateway credentials (primary SMS provider)
  - NEW: M-Pesa Daraja API credentials (payment integration)
  - NEW: default_sms_provider setting ("africas_talking" or "twilio")
  - NEW: daily_sms_budget_kes (cost control, default 500)
  - NEW: business_hours_start / business_hours_end (default 7-19)
  - Twilio fields kept as fallback (international routing, testing)
  - database_url and redis_url kept as required secrets

KEY DESIGN DECISIONS
--------------------
  1. SecretStr for all sensitive values - renders as masked in logs.
  2. lru_cache on get_settings() - parses env only once per process.
  3. env_file=".env" for local dev; env vars override in production.
  4. extra="ignore" - unknown env vars don't crash the app.
  5. mpesa_env is "sandbox" or "production" - controls Daraja API endpoint.

TEACHING NOTES
--------------
  - SecretStr.get_secret_value() retrieves the actual string when
    needed (e.g., when constructing the database URL). Never log the
    return value of get_secret_value().
  - The lru_cache decorator means we parse the environment only once
    per process. This is important for Celery workers.
  - Field validation (ge, le, pattern) catches bad config at startup.

HOW THIS CONNECTS TO OTHER MODULES
----------------------------------
  - infra/database.py reads database_url for the async engine.
  - infra/redis_pool.py reads redis_url for the Redis client.
  - infra/connectivity_watcher.py reads africas_talking_* to ping the API.
  - domain/templates.py reads max_sms_segments for segment counting.
  - domain/policy_service.py reads business_hours_* for defaults.
  - adapters/africas_talking.py reads africas_talking_* credentials.
  - adapters/mpesa_adapter.py reads mpesa_* credentials.
======================================================================
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings, loaded from environment variables / .env file.

    Every field has a description for documentation. Required fields
    use Ellipsis (...) - the app won't start without them.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # allow unknown env vars (don't crash on Pi)
    )

    # == Application ==
    app_env: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Controls logging level and error detail exposure",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
    )
    business_timezone: str = Field(
        default="Africa/Nairobi",
        description="IANA timezone for scheduling (Kenya = EAT, UTC+3, no DST)",
    )

    # == Database ==
    database_url: SecretStr = Field(
        ...,  # required (no default)
        description="PostgreSQL async connection string (asyncpg driver)",
    )

    # == Redis ==
    redis_url: SecretStr = Field(
        ...,  # required
        description="Redis connection string for Celery broker and cache",
    )

    # == SMS Provider Selection ==
    default_sms_provider: Literal["africas_talking", "twilio", "mock"] = Field(
        default="africas_talking",
        description="Primary SMS gateway: Africa's Talking (Kenya), Twilio (fallback), mock (test)",
    )

    # == Africa's Talking (primary SMS gateway for Kenya) ==
    africas_talking_username: str = Field(
        default="",
        description="Africa's Talking API username (sandbox or production)",
    )
    africas_talking_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Africa's Talking API key",
    )
    africas_talking_sender_id: str = Field(
        default="",
        description="Alphanumeric sender ID registered with Africa's Talking (e.g., MAMA-MBOGA)",
    )

    # == Twilio (fallback SMS gateway, kept from tuition agent) ==
    twilio_account_sid: SecretStr = Field(
        default=SecretStr(""),
        description="Twilio Account SID (starts with AC). Optional fallback.",
    )
    twilio_auth_token: SecretStr = Field(
        default=SecretStr(""),
        description="Twilio Auth Token. Optional fallback.",
    )
    twilio_phone_number: str = Field(
        default="",
        description="Twilio phone number in E.164 format. Optional fallback.",
    )

    # == M-Pesa Daraja API (payment integration) ==
    mpesa_consumer_key: SecretStr = Field(
        default=SecretStr(""),
        description="Safaricom Daraja API consumer key",
    )
    mpesa_consumer_secret: SecretStr = Field(
        default=SecretStr(""),
        description="Safaricom Daraja API consumer secret",
    )
    mpesa_short_code: str = Field(
        default="",
        description="M-Pesa Paybill or Till number for the business",
    )
    mpesa_passkey: SecretStr = Field(
        default=SecretStr(""),
        description="M-Pesa Lipa na M-Pesa Online passkey (from Daraja portal)",
    )
    mpesa_env: Literal["sandbox", "production"] = Field(
        default="sandbox",
        description="Safaricom Daraja API environment",
    )

    # == Webhook Security ==
    webhook_secret: SecretStr = Field(
        ...,  # required
        description="Shared secret to verify inbound webhook signatures",
    )

    # == Backup / Encryption ==
    backup_encryption_key: SecretStr = Field(
        default=SecretStr(""),
        description="32-byte hex key for backup encryption",
    )

    # == Admin Dashboard ==
    admin_token: str = Field(
        default="",
        description="Token for admin dashboard authentication (X-Admin-Token header)",
    )

    # == Operational (inherited + adapted) ==
    max_sms_segments: int = Field(
        default=2,
        description="Maximum SMS segments per message (1 = 160 chars GSM-7)",
    )
    quiet_hours_start: int = Field(
        default=20,
        description="Hour (24h) when quiet hours begin (Kenya default 20:00)",
        ge=0,
        le=23,
    )
    quiet_hours_end: int = Field(
        default=7,
        description="Hour (24h) when quiet hours end (Kenya default 07:00)",
        ge=0,
        le=23,
    )
    # NEW: business hours (separate from quiet hours)
    business_hours_start: int = Field(
        default=7,
        description="Hour (24h) when business hours begin (default 07:00)",
        ge=0,
        le=23,
    )
    business_hours_end: int = Field(
        default=19,
        description="Hour (24h) when business hours end (default 19:00)",
        ge=0,
        le=23,
    )
    reminder_retry_max: int = Field(
        default=3,
        description="Maximum send retries per message",
        ge=0,
        le=10,
    )
    unknown_delivery_reconcile_minutes: int = Field(
        default=10,
        description="Minutes between reconciliation checks",
        ge=1,
    )
    # NEW: daily SMS budget in KES (cost control)
    daily_sms_budget_kes: int = Field(
        default=500,
        description="Daily SMS spend cap in KES. Worker pauses when exceeded.",
        ge=0,
    )
    # NEW: max promotional SMS per customer per week (Kenya CA guideline: 3)
    max_promo_per_week: int = Field(
        default=3,
        description="Max promotional SMS per customer per week (Kenya CA: 3)",
        ge=0,
        le=10,
    )

    # == Connectivity Watcher ==
    connectivity_check_interval_seconds: int = Field(
        default=30,
        description="Seconds between connectivity pings to Africa's Talking API",
        ge=5,
    )

    # ── v2 Additions (efficiency, security, resilience) ──

    # E7: Database connection pool sizing
    database_pool_size: int = Field(
        default=5,
        description="SQLAlchemy connection pool size (workers may need more)",
        ge=1,
        le=50,
    )
    database_max_overflow: int = Field(
        default=10,
        description="Max overflow connections beyond pool_size",
        ge=0,
        le=50,
    )

    # S6: CORS allowed origins (comma-separated)
    cors_allowed_origins: str = Field(
        default="http://localhost:3000,http://localhost:8000",
        description="Comma-separated list of allowed CORS origins",
    )

    # R9: Admin alert phone for failure threshold SMS alerts
    admin_alert_phone: str = Field(
        default="",
        description="Phone number (E.164) to receive failure-rate SMS alerts",
    )

    # R10: Backup output directory
    backup_output_dir: str = Field(
        default="/data/backups",
        description="Directory for encrypted database backups",
    )

    # S8: TLS domain for production certificates
    tls_domain: str = Field(
        default="",
        description="Domain for TLS certificates in production (empty = no TLS)",
    )


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings instance.

    lru_cache means we parse the environment only once per process.
    This is important for Celery workers (they import settings frequently).
    """
    return Settings()