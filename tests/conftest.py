"""
tests/conftest.py — Shared pytest fixtures for the SMS-First Agent
═══════════════════════════════════════════════════════════════════════

PURPOSE
-------
Provides reusable test fixtures:
  - In-memory SQLite database (fast, no PostgreSQL needed for unit tests)
  - Mock SMS adapter (no real SMS sends during testing)
  - Sample business, customer, contact, transaction data factories
  - Async DB session fixture (with nested transaction rollback)

ADAPTATIONS FROM THE TUITION AGENT
----------------------------------
  - Sample data uses Business/Customer/Contact/Transaction (not School/Student/Guardian/Invoice)
  - Mock SMS adapter is the same (inherited, provider-agnostic)
  - Phone numbers use Kenyan format (+2547XXXXXXXXX) instead of US (+1...)
  - Amounts are in KES (not USD)
  - NEW: sample campaign + segment fixtures for campaign tests
  - NEW: sample M-Pesa payment fixture for mpesa tests

KEY DESIGN DECISIONS
--------------------
  1. In-memory SQLite for unit tests: fast, no Docker/PostgreSQL needed.
     SQLite supports the core SQLAlchemy operations we test.
     NOTE: SQLite doesn't support FOR UPDATE SKIP LOCKED — integration
     tests that need row locking use a real PostgreSQL test container.
  2. Mock SMS adapter: always returns ACCEPTED, stores sent messages
     in a list so tests can assert what was "sent".
  3. Nested transaction rollback: each test runs in a SAVEPOINT that is
     rolled back after the test, keeping the schema clean.
  4. Factory fixtures: each test gets fresh sample data (no shared state
     between tests — isolation is critical for reliable test suites).

TEACHING NOTES
--------------
  - `pytest_asyncio.fixture` creates async fixtures that work with
    `async def test_...` functions.
  - `scope="session"` creates the fixture once per test session (fast).
  - `scope="function"` (default) creates a fresh fixture per test (isolated).
  - The mock adapter's `get_sent_messages()` lets tests assert what was
    sent without hitting a real SMS provider.
  - We set APP_ENV=development and DATABASE_URL to SQLite in-memory so
    the settings module doesn't fail on missing env vars.

KENYA-SPECIFIC CONSIDERATIONS
-----------------------------
  - All sample phone numbers use +254 prefix (Kenya).
  - Amounts are in KES (Kenya Shillings).
  - preferred_language defaults to "en" (English).
  - Business timezone is Africa/Nairobi.
═══════════════════════════════════════════════════════════════════════
"""

import os
from datetime import date, datetime
from decimal import Decimal

# ── Set test environment variables BEFORE importing app modules ──
# These must be set before any `from infra.settings import get_settings`
# call, because pydantic-settings reads env vars at import time.
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN_HASH", "")
os.environ.setdefault("DEFAULT_SMS_PROVIDER", "mock")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from adapters.mock_adapter import MockAdapter
from domain.models import (
    Base,
    Business,
    Campaign,
    CampaignStatus,
    Contact,
    Customer,
    CustomerSegment,
    Language,
    OutboundMessage,
    Payment,
    PaymentStatus,
    SegmentMember,
    Transaction,
    TransactionStatus,
    TransactionType,
    MpesaPayment,
)


# ── Database fixtures ─────────────────────────────────────────────


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """
    Creates a fresh in-memory SQLite database once per test session.

    TEACHING NOTE: We use SQLite in-memory for unit tests because:
      - No Docker/PostgreSQL installation needed
      - Tests run in < 1 second
      - SQLAlchemy abstracts the SQL dialect differences

    Integration tests that need PostgreSQL-specific features (FOR UPDATE
    SKIP LOCKED, ON CONFLICT DO NOTHING) use a separate fixture that
    connects to a test PostgreSQL instance.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine):
    """
    Yields an async DB session with nested transaction rollback.

    Each test gets a clean session. After the test, the nested
    transaction (SAVEPOINT) is rolled back, so the schema is preserved
    but the data is cleared — no leakage between tests.

    TEACHING NOTE: This pattern is called "nested transaction rollback".
    It's the fastest way to isolate database tests:
      1. Begin a SAVEPOINT
      2. Run the test (which may insert/update/delete)
      3. Rollback the SAVEPOINT (undoes all test changes)
      4. The schema is still there for the next test
    """
    async with test_engine.connect() as conn:
        trans = await conn.begin_nested()
        session_factory = async_sessionmaker(
            conn, expire_on_commit=False, class_=AsyncSession
        )
        async with session_factory() as session:
            yield session
        await trans.rollback()


# ── Mock SMS adapter fixture ───────────────────────────────────────


@pytest.fixture
def mock_adapter():
    """
    Mock SMS adapter that always succeeds (success_rate=1.0).

    Stores all "sent" messages in a list so tests can assert:
      adapter.get_sent_messages() → [{"to": "...", "body": "...", ...}]

    TEACHING NOTE: The mock adapter implements the same SMSAdapter
    interface as Africa's Talking and Twilio. Tests use the mock so
    they don't send real SMS (which costs money and needs credentials).
    """
    return MockAdapter(success_rate=1.0)


@pytest.fixture
def failing_adapter():
    """Mock SMS adapter that always fails (for testing error handling)."""
    return MockAdapter(success_rate=0.0)


# ── Sample data factories ──────────────────────────────────────────


@pytest.fixture
def sample_business():
    """
    A sample Business (Kenyan small business).

    TEACHING NOTE: We use `make()` pattern instead of DB insertion.
    The test inserts it if needed. This keeps the fixture pure (no side effects).
    """
    return Business(
        id=1,
        name="Test Duka",
        timezone="Africa/Nairobi",
        business_type="retail",
        sms_opt_in_default=True,
        sender_id="TEST-DUKA",
    )


@pytest.fixture
def sample_customer():
    """A sample Customer with English preference and 500 loyalty points."""
    return Customer(
        id=101,
        business_id=1,
        first_name="Wanjiru",
        last_name="K",
        email="wanjiru@example.com",
        loyalty_points=500,
        preferred_language=Language.EN,
    )


@pytest.fixture
def sample_contact():
    """
    A sample Contact with a Kenyan phone number.

    Phone is in E.164 format: +254XXXXXXXXX (Kenya country code = 254).
    """
    return Contact(
        id=201,
        business_id=1,
        first_name="Wanjiru",
        phone="+254712345678",
        sms_opt_in=True,
    )


@pytest.fixture
def sample_transaction():
    """
    A sample CREDIT transaction (customer owes KES 2,500).

    due_date is set to a future date (30 days from "today") so reminder
    tests can check DUE_14, DUE_3, DUE_TODAY logic.

    TEACHING NOTE: We use CREDIT type because it has the full reminder
    cadence (14/3/today + late notice). SALE has no reminders.
    """
    return Transaction(
        id=1001,
        business_id=1,
        customer_id=101,
        contact_id=201,
        transaction_number="CRED-001",
        type=TransactionType.CREDIT,
        amount_due=Decimal("2500.00"),
        amount_paid=Decimal("0.00"),
        due_date=date(2026, 7, 15),  # 30 days from "today" (test date)
        status=TransactionStatus.PENDING,
    )


@pytest.fixture
def sample_layaway_transaction():
    """A sample LAYAWAY transaction (installment purchase)."""
    return Transaction(
        id=1002,
        business_id=1,
        customer_id=101,
        contact_id=201,
        transaction_number="LAY-001",
        type=TransactionType.LAYAWAY,
        amount_due=Decimal("10000.00"),
        amount_paid=Decimal("4000.00"),
        due_date=date(2026, 8, 1),
        status=TransactionStatus.PARTIAL,
    )


@pytest.fixture
def sample_service_transaction():
    """A sample SERVICE transaction (appointment)."""
    return Transaction(
        id=1003,
        business_id=1,
        customer_id=101,
        contact_id=201,
        transaction_number="APT-001",
        type=TransactionType.SERVICE,
        amount_due=Decimal("500.00"),
        amount_paid=Decimal("500.00"),  # service already paid
        due_date=date(2026, 6, 20),  # appointment date
        status=TransactionStatus.PAID,
    )


@pytest.fixture
def sample_mpesa_payment():
    """
    A sample M-Pesa payment record (C2B webhook).

    The mpesa_ref is a Safaricom confirmation code (globally unique).
    The phone matches the sample_contact's phone for testing matching.
    """
    return MpesaPayment(
        id=1,
        business_id=1,
        mpesa_ref="SI7K2P9X4",
        phone="+254712345678",
        amount=Decimal("2500.00"),
        account_ref="CRED-001",  # matches sample_transaction.transaction_number
        source="c2b",
    )


@pytest.fixture
def sample_segment():
    """A sample customer segment for campaign testing."""
    return CustomerSegment(
        id=1,
        business_id=1,
        name="VIP Customers",
        description="High-value repeat customers",
    )


@pytest.fixture
def sample_campaign():
    """A sample promotional campaign."""
    return Campaign(
        id=1,
        business_id=1,
        segment_id=1,
        name="June Sale",
        template_name="promo_message",
        status=CampaignStatus.SCHEDULED,
        schedule_start=datetime(2026, 6, 17, 10, 0, 0),
        schedule_end=datetime(2026, 6, 20, 18, 0, 0),
        max_per_customer_per_week=3,
    )


@pytest.fixture
def sample_payment():
    """A sample confirmed payment record."""
    return Payment(
        id=1,
        transaction_id=1001,
        amount=Decimal("500.00"),
        status=PaymentStatus.CONFIRMED,
        payment_method="mpesa_c2b",
        external_reference="SI7K2P9X4",
        confirmed_by="mpesa_webhook",
        confirmed_at=datetime.utcnow(),
    )


@pytest.fixture
def sample_outbound_message():
    """A sample pending outbound message in the outbox."""
    return OutboundMessage(
        id=1,
        business_id=1,
        contact_id=201,
        transaction_id=1001,
        message_key="1:101:201:1001:due_14:2026-07-15:v1",
        reminder_type="due_14",
        status="pending",
        body="",
        segments=1,
        language="en",
        provider="africas_talking",
        client_message_id="1:101:201:1001:due_14:2026-07-15:v1",
        retry_count=0,
        max_retries=3,
        scheduled_at=datetime.utcnow(),
    )


# ── HTTP client fixture (for API tests) ────────────────────────────


@pytest_asyncio.fixture
async def api_client():
    """
    Async HTTP client for testing FastAPI endpoints.

    TEACHING NOTE: We use httpx.AsyncClient with the ASGI transport.
    This lets us test the FastAPI app without starting a real server.
    """
    try:
        from httpx import AsyncClient, ASGITransport
        from api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    except ImportError:
        # httpx or api.main not available — skip API tests
        pytest.skip("httpx or api.main not available")


# ── Pytest configuration ───────────────────────────────────────────


def pytest_collection_modifyitems(config, items):
    """
    Automatically mark async tests with asyncio.

    TEACHING NOTE: pytest-asyncio requires the `@pytest.mark.asyncio`
    marker on async test functions. This hook adds it automatically
    so we don't need to decorate every test function.
    """
    for item in items:
        if hasattr(item, "function") and hasattr(item.function, "__code__"):
            if item.function.__code__.co_flags & 0x100:  # CO_COROUTINE
                item.add_marker(pytest.mark.asyncio)