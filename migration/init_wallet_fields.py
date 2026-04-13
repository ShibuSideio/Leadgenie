"""
Sideio Leads — Credit Wallet Schema Migration
==============================================
Run once against production Firestore BEFORE deploying the patched services.

What it does:
  1. For each user document, reads all wallet_shards sub-collection documents
     and sums their consumed_credits into wallet.total_consumed (authoritative).
  2. Sets wallet.reserved_credits = 0 (in-flight reservation counter).

After this migration:
  - wallet.total_consumed is the source of truth for the credit reservation check.
  - wallet.reserved_credits tracks in-flight pipeline tasks (pre-debited).
  - wallet_shards continue to be written for analytics (unchanged behavior).

Usage:
  GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json python migration/init_wallet_fields.py
  
  Or from Cloud Shell:
  python migration/init_wallet_fields.py --project sideio-leads-v19
"""

import os
import sys
import argparse
from google.cloud import firestore

def run_migration(project_id: str, dry_run: bool = False):
    db = firestore.Client(project=project_id)

    users_stream = list(db.collection("users").stream())
    print(f"Found {len(users_stream)} user documents. dry_run={dry_run}")

    migrated   = 0
    skipped    = 0
    errored    = 0

    for user_doc in users_stream:
        uid  = user_doc.id
        data = user_doc.to_dict() or {}

        # --- Calculate current shard sum (source of truth for consumed) ---
        try:
            shard_sum = sum(
                int(s.to_dict().get("consumed_credits", 0) or 0)
                for s in db.collection("users").document(uid)
                           .collection("wallet_shards").stream()
            )
        except Exception as shard_err:
            print(f"  [ERROR] {uid}: shard read failed: {shard_err}")
            errored += 1
            continue

        wallet = data.get("wallet", {})
        updates = {}

        if "total_consumed" not in wallet:
            updates["wallet.total_consumed"] = shard_sum
        else:
            print(f"  [SKIP]  {uid}: total_consumed already set "
                  f"({wallet['total_consumed']}), leaving untouched.")
            skipped += 1
            continue  # Don't overwrite if already migrated

        if "reserved_credits" not in wallet:
            updates["wallet.reserved_credits"] = 0

        allocated = int(wallet.get("allocated_credits", 0) or 0)
        print(f"  [MIGRATE] {uid}: allocated={allocated}, "
              f"shard_sum={shard_sum}, reserved_credits=0")

        if not dry_run and updates:
            try:
                db.collection("users").document(uid).update(updates)
                migrated += 1
            except Exception as write_err:
                print(f"  [ERROR] {uid}: write failed: {write_err}")
                errored += 1
        elif dry_run:
            print(f"  [DRY-RUN] Would update: {updates}")
            migrated += 1

    print(f"\nMigration complete: migrated={migrated} skipped={skipped} errors={errored}")

    if errored > 0:
        print("WARNING: Some documents failed. Re-run to retry errored docs.")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wallet schema migration")
    parser.add_argument(
        "--project",
        default=os.environ.get("PROJECT_ID", "sideio-leads-v19"),
        help="GCP Project ID"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changes without writing to Firestore"
    )
    args = parser.parse_args()
    run_migration(project_id=args.project, dry_run=args.dry_run)
