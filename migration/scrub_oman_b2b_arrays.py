#!/usr/bin/env python3
"""
Database Scrub: Clear zombie B2B data arrays from Oman Reality campaign.

Clears `pain_points`, `features`, and `value_propositions` arrays that
still contain legacy B2B corporate strings (e.g., "Weak brand story",
"Unclear positioning") which leak into historical_str and pollute
B2C query construction.

Usage:
  python scrub_oman_b2b_arrays.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json

import firebase_admin  # type: ignore
from firebase_admin import firestore  # type: ignore

CAMPAIGN_ID = "ZU56iQlGMiWVeAm8DdAf"
FIELDS_TO_CLEAR = ["pain_points", "features", "value_propositions"]


def main():
    parser = argparse.ArgumentParser(description="Scrub B2B arrays from Oman campaign.")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing.")
    args = parser.parse_args()

    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firestore.client()

    ref = db.collection("campaigns").document(CAMPAIGN_ID)
    snap = ref.get()

    if not snap.exists:
        print(f"Campaign {CAMPAIGN_ID} NOT FOUND.")
        return

    data = snap.to_dict()
    campaign_name = data.get("name", "Unknown")
    vector = data.get("sourcing_vector", "MISSING")

    print(f"\n{'='*60}")
    print(f"  SCRUB TARGET: {campaign_name}")
    print(f"  Campaign ID:  {CAMPAIGN_ID}")
    print(f"  Vector:       {vector}")
    print(f"{'='*60}\n")

    update_payload = {}
    for field in FIELDS_TO_CLEAR:
        current = data.get(field, [])
        if current:
            print(f"  [{field}] CURRENT ({len(current)} items):")
            for item in current[:5]:
                print(f"    - {str(item)[:80]}")
            if len(current) > 5:
                print(f"    ... and {len(current) - 5} more")
            update_payload[field] = []
        else:
            print(f"  [{field}] Already empty. No action needed.")

    if not update_payload:
        print(f"\n  All fields already clean. Nothing to do.")
        return

    print(f"\n  Fields to clear: {list(update_payload.keys())}")

    if args.dry_run:
        print(f"\n  DRY RUN: Would clear {len(update_payload)} field(s).")
    else:
        ref.update(update_payload)
        print(f"\n  PATCHED: {len(update_payload)} field(s) set to [].")

    print(f"\n{'='*60}")
    print(f"  DONE {'(DRY RUN)' if args.dry_run else ''}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
