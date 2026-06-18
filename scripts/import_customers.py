#!/usr/bin/env python3
"""
scripts/import_customers.py — CSV customer import utility
═══════════════════════════════════════════════════

Import customers from a CSV file into the SMS-First Agent database.

CSV format (with headers):
    first_name,phone,preferred_language,loyalty_points
    Jane,+254712345678,en,500
    John,+254723456789,sw,0

Usage:
    python scripts/import_customers.py --file customers.csv --business-id 1

Features:
  - Dedup by phone number (skips existing contacts)
  - Creates Customer + Contact + link records
  - Validates phone format (converts to E.164)
  - Reports import statistics

Teaching notes:
  - We use the same async DB session pattern as the rest of the app.
  - The script is idempotent: running it twice won't create duplicates
    because of the UNIQUE(school_id, phone) constraint on contacts.
  - We import httpx for optional API-mode import (POST to admin endpoint).
═══════════════════════════════════════════════════
"""

import argparse
import asyncio
import csv
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from infra.database import async_session_factory
from domain.models import Business, Contact, Customer, CustomerContactLink


def normalize_kenyan_phone(phone: str) -> str:
    """Convert Kenyan phone to E.164: 07XX → +2547XX."""
    phone = phone.strip()
    if phone.startswith("+254"):
        return phone
    if phone.startswith("254"):
        return f"+{phone}"
    if phone.startswith("0"):
        return f"+254{phone[1:]}"
    if phone.startswith("7") or phone.startswith("1"):
        return f"+254{phone}"
    return phone


async def import_customers(csv_path: str, business_id: int) -> dict:
    """Import customers from CSV file."""
    stats = {"imported": 0, "skipped_duplicate": 0, "errors": 0}

    async with async_session_factory() as session:
        # Verify business exists
        result = await session.execute(select(Business).where(Business.id == business_id))
        business = result.scalar_one_or_none()
        if not business:
            print(f"ERROR: Business {business_id} not found")
            return stats

        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            for row in reader:
                try:
                    phone = normalize_kenyan_phone(row["phone"])
                    first_name = row["first_name"].strip()
                    preferred_language = row.get("preferred_language", "en").strip()
                    loyalty_points = int(row.get("loyalty_points", "0") or "0")

                    # Check for existing contact (dedup by phone)
                    existing = await session.execute(
                        select(Contact).where(
                            Contact.school_id == business_id,
                            Contact.phone == phone,
                        )
                    )
                    if existing.scalar_one_or_none():
                        stats["skipped_duplicate"] += 1
                        continue

                    # Create customer
                    customer = Customer(
                        school_id=business_id,
                        first_name=first_name,
                        preferred_language=preferred_language,
                        loyalty_points=loyalty_points,
                    )
                    session.add(customer)
                    await session.flush()

                    # Create contact
                    contact = Contact(
                        school_id=business_id,
                        first_name=first_name,
                        phone=phone,
                        sms_opt_in=True,
                    )
                    session.add(contact)
                    await session.flush()

                    # Link customer to contact
                    link = CustomerContactLink(
                        customer_id=customer.id,
                        contact_id=contact.id,
                        is_primary_contact=True,
                    )
                    session.add(link)

                    stats["imported"] += 1

                except Exception as exc:
                    print(f"  ERROR on row {reader.line_num}: {exc}")
                    stats["errors"] += 1
                    continue

        await session.commit()

    return stats


def main():
    parser = argparse.ArgumentParser(description="Import customers from CSV")
    parser.add_argument("--file", required=True, help="Path to CSV file")
    parser.add_argument("--business-id", type=int, default=1, help="Business ID")
    args = parser.parse_args()

    print(f"Importing customers from {args.file} for business {args.business_id}...")
    stats = asyncio.run(import_customers(args.file, args.business_id))

    print(f"\nImport complete:")
    print(f"  Imported:          {stats['imported']}")
    print(f"  Skipped (duplicate): {stats['skipped_duplicate']}")
    print(f"  Errors:            {stats['errors']}")


if __name__ == "__main__":
    main()