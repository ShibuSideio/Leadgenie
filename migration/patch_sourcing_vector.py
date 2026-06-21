#!/usr/bin/env python3
"""
Firestore Patch: Migrate legacy sourcing_vector values to archetypes.

Targets:
  1. Campaign ZU56iQlGMiWVeAm8DdAf — Force to "B2C" immediately.
  2. Any other campaigns with legacy values ("Classic B2B",
     "Social/Forum Listening", "Review Hijacking", "Maps/GMB Targeting")
     — Log them for manual review.

Usage:
  python patch_sourcing_vector.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys

# Firebase Admin SDK
import firebase_admin  # type: ignore
from firebase_admin import credentials, firestore  # type: ignore


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OMAN_CAMPAIGN_ID = "ZU56iQlGMiWVeAm8DdAf"
LEGACY_VECTORS = {"Classic B2B", "Social/Forum Listening", "Review Hijacking", "Maps/GMB Targeting"}
NEW_ARCHETYPES = {"B2B", "B2C", "B2B2C", "D2C"}


def main():
    parser = argparse.ArgumentParser(description="Patch legacy sourcing_vector values.")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing.")
    args = parser.parse_args()

    # Initialize Firebase Admin (uses ADC or GOOGLE_APPLICATION_CREDENTIALS)
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firestore.client()

    # -- 1. Direct patch: Oman Reality campaign --------------------------------
    print(f"\n{'='*60}")
    print(f"  PATCH TARGET: Campaign {OMAN_CAMPAIGN_ID}")
    print(f"{'='*60}")

    oman_ref = db.collection("campaigns").document(OMAN_CAMPAIGN_ID)
    oman_snap = oman_ref.get()

    if not oman_snap.exists:
        print(f"  Campaign {OMAN_CAMPAIGN_ID} NOT FOUND. Skipping.")
    else:
        oman_data = oman_snap.to_dict()
        current_vector = oman_data.get("sourcing_vector", "MISSING")
        campaign_name = oman_data.get("name", "Unknown")
        bio_preview = (oman_data.get("bio", "") or "")[:80]

        print(f"  Name:            {campaign_name}")
        print(f"  Current vector:  {current_vector}")
        print(f"  Bio preview:     {bio_preview}...")

        if current_vector == "B2C":
            print(f"  Already set to B2C. No action needed.")
        else:
            if args.dry_run:
                print(f"  DRY RUN: Would update sourcing_vector -> 'B2C'")
            else:
                oman_ref.update({"sourcing_vector": "B2C"})
                print(f"  PATCHED: sourcing_vector -> 'B2C'")

    # -- 2. Scan all campaigns with legacy vectors -----------------------------
    print(f"\n{'='*60}")
    print(f"  LEGACY VECTOR SCAN")
    print(f"{'='*60}")

    legacy_count = 0
    for legacy_val in sorted(LEGACY_VECTORS):
        query = db.collection("campaigns").where("sourcing_vector", "==", legacy_val).stream()
        for doc in query:
            legacy_count += 1
            d = doc.to_dict()
            print(f"  [{doc.id}] vector='{legacy_val}' "
                  f"name='{d.get('name', 'N/A')[:40]}' "
                  f"status='{d.get('status', 'N/A')}' "
                  f"tenant='{d.get('tenant_id', 'N/A')[:12]}...'")

    if legacy_count == 0:
        print("  No campaigns with legacy vectors found.")
    else:
        print(f"\n  {legacy_count} campaign(s) still have legacy sourcing_vector values.")
        print("  These will continue to work (backwards-compatible) but will not")
        print("  trigger consumer routing. Re-classify via the campaign update API")
        print("  or update sourcing_vector manually.")

    print(f"\n{'='*60}")
    print(f"  DONE {'(DRY RUN)' if args.dry_run else ''}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
