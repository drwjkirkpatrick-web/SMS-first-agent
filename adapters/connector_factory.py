"""
adapters/connector_factory.py — Connector Instantiation
═══════════════════════════════════════════════════

Maps adapter_type strings to concrete connector classes.

Adapted from the original tuition agent's connector_factory.py:
  - "csv" → CSVConnector (customer/transaction CSV import)
  - "pos" → POSConnector (stub for future POS API integration)
  - "mpesa" → MpesaAdapter (M-Pesa payment integration)

Example:
    connector = get_connector(business_id=1, adapter_type="csv", config={...})

Teaching notes:
  - The factory pattern decouples connector creation from usage.
    Workers call `get_connector()` and don't need to know the concrete class.
  - New connectors are added by registering them in _CONNECTOR_REGISTRY.
  - The registry maps string keys to classes, making it easy to add
    new data sources without changing existing code (Open/Closed Principle).

Kenya-specific considerations:
  - CSV is the primary connector (most small businesses use Excel).
  - POS integration is a stub — future implementation would connect to
    specific Kenyan POS systems (e.g., local providers).
  - M-Pesa adapter is registered for payment reconciliation, though it
    doesn't implement the CRMConnector interface (it's a payment adapter,
    not a data sync connector). We handle it separately.
═══════════════════════════════════════════════════
"""

from typing import Optional

from adapters.csv_connector import CSVConnector
from adapters.crm_connector import CRMConnector


# ── POS Connector Stub ──────────────────────────────────────────
#
# This is a placeholder for future POS API integration.
# Kenyan POS systems vary — some have APIs, some only export CSV.
# A real implementation would extend CRMConnector and implement
# sync_customers/sync_transactions via REST API calls.

class POSConnector(CRMConnector):
    """
    Stub POS connector for future implementation.

    A real POS connector would:
    1. Authenticate with the POS system's API (OAuth, API key, etc.)
    2. Fetch customer data via GET /api/customers
    3. Fetch transaction data via GET /api/transactions
    4. Map POS-specific fields to our CustomerRecord/TransactionRecord
    5. Handle pagination (cursor or offset-based)

    For now, it raises NotImplementedError for all methods.
    """

    async def get_checkpoint(self):
        raise NotImplementedError("POSConnector not yet implemented")

    async def save_checkpoint(self, checkpoint):
        raise NotImplementedError("POSConnector not yet implemented")

    async def sync_customers(self, checkpoint):
        raise NotImplementedError("POSConnector not yet implemented")
        yield  # type: ignore[unreachable]

    async def sync_contacts(self, checkpoint):
        raise NotImplementedError("POSConnector not yet implemented")
        yield  # type: ignore[unreachable]

    async def sync_transactions(self, checkpoint):
        raise NotImplementedError("POSConnector not yet implemented")
        yield  # type: ignore[unreachable]

    async def sync_payments(self, checkpoint):
        raise NotImplementedError("POSConnector not yet implemented")
        yield  # type: ignore[unreachable]

    async def test_connection(self) -> bool:
        raise NotImplementedError("POSConnector not yet implemented")


# ── Connector Registry ──────────────────────────────────────────

_CONNECTOR_REGISTRY: dict[str, type[CRMConnector]] = {
    "csv": CSVConnector,
    "pos": POSConnector,
    # Future: "quickbooks": QuickBooksConnector,
    # Future: "lightspeed": LightspeedConnector,
    # Future: "square": SquareConnector,
}


def get_connector(
    business_id: int,
    adapter_type: str,
    config: dict,
) -> Optional[CRMConnector]:
    """
    Factory function: returns a configured connector instance.

    Args:
        business_id: The business to sync data for
        adapter_type: Connector type key (e.g., "csv", "pos")
        config: Connection configuration dict

    Returns:
        Configured connector instance, or None if type not registered.
    """
    connector_cls = _CONNECTOR_REGISTRY.get(adapter_type)
    if connector_cls is None:
        return None
    return connector_cls(business_id=business_id, config=config)


def list_available_connectors() -> list[str]:
    """Return all registered connector type names."""
    return list(_CONNECTOR_REGISTRY.keys())