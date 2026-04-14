"""
migration/flush_stale_state.py
===============================
One-time state migration script. Clears poisoned global_lead_locks and
scraped_cache collections left by a prior production bug.

EXECUTION:
  Run ONCE via a dedicated, audited Cloud Run Job or an authorised Cloud Shell
  session linked to a service account with roles/datastore.owner.

  gcloud run jobs execute flush-stale-state \
      --project=sideio-leads-v16 \
      --region=asia-south1

SAFETY GUARANTEES:
  - Batch size capped at 400 (Firestore limit = 500 ops/batch; 400 gives headroom)
  - Each batch is committed atomically; partial failures do not leave split state
  - DRY_RUN env var: set to "true" to log what WOULD be deleted without writing
  - Fully logged to stdout → captured by Cloud Logging under the job's log stream
  - Idempotent: safe to re-run; deleting an already-deleted doc is a no-op in Firestore

AUTHOR: Lead Data Engineer — Sideio Platform
CREATED: 2026-04-14
"""

import os
import sys
import datetime
import logging

from google.cloud import firestore

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ID = os.environ.get("PROJECT_ID", "sideio-leads-v16")
BATCH_SIZE = 400          # Max 500 ops per Firestore write batch; 400 adds safety margin
DRY_RUN    = os.environ.get("DRY_RUN", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Logging — structured output so Cloud Logging indexes severity correctly
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("flush_stale_state")

# ---------------------------------------------------------------------------
# Firestore client
# ---------------------------------------------------------------------------
db = firestore.Client(project=PROJECT_ID)


# ---------------------------------------------------------------------------
# Core utility: chunked batch delete
# ---------------------------------------------------------------------------
def _batch_delete_collection(collection_name: str) -> int:
    """
    Safely deletes ALL documents in `collection_name` using batched writes.

    Algorithm:
      1. Stream documents in pages of BATCH_SIZE using .limit().stream()
      2. Accumulate references into a write batch
      3. Commit the batch atomically
      4. Repeat until the page is empty (collection is exhausted)

    This avoids loading the entire collection into memory (which would OOM
    a Cloud Run Job container on large collections) and respects Firestore's
    500-operations-per-batch hard limit.

    Returns: total document count deleted.
    """
    col_ref      = db.collection(collection_name)
    total        = 0
    batch_number = 0

    log.info(f"[{collection_name}] Starting deletion. DRY_RUN={DRY_RUN}")

    while True:
        # Fetch the next page — .limit() on .stream() is O(BATCH_SIZE) RAM
        docs = list(col_ref.limit(BATCH_SIZE).stream())

        if not docs:
            log.info(f"[{collection_name}] Collection exhausted. "
                     f"Total documents deleted: {total}")
            break

        batch_number += 1
        batch_count  = len(docs)

        if DRY_RUN:
            log.info(
                f"[{collection_name}] [DRY RUN] Would delete batch #{batch_number} "
                f"({batch_count} docs). First ID: {docs[0].id}"
            )
            # In dry-run we still need to advance past this page.
            # We can't re-query the same limit without a cursor — break to be safe.
            total += batch_count
            log.info(f"[{collection_name}] [DRY RUN] Dry run complete. "
                     f"Estimated total: {total}")
            break

        write_batch = db.batch()
        for doc in docs:
            write_batch.delete(doc.reference)

        try:
            write_batch.commit()
            total += batch_count
            log.info(
                f"[{collection_name}] Deleted batch #{batch_number} "
                f"({batch_count} docs). Running total: {total}"
            )
        except Exception as commit_err:
            log.error(
                f"[{collection_name}] Batch #{batch_number} commit FAILED: {commit_err}. "
                f"Aborting. {total} documents were already deleted successfully."
            )
            raise  # Re-raise so the Cloud Run Job exits non-zero → Cloud Scheduler alerts

    return total


# ---------------------------------------------------------------------------
# Target 1: global_lead_locks
# ---------------------------------------------------------------------------
def flush_global_lead_locks() -> int:
    """
    Deletes all documents in the global_lead_locks collection.

    These documents hold 14-day exclusivity locks on domains/social paths.
    A prior bug caused orphaned locks (no corresponding lead, no expiry update),
    permanently blocking those domains from being re-processed by any tenant.

    Safe to flush entirely: the next dispatch() run will re-acquire locks
    atomically via _acquire_lead_lock(@_firestore_transactional).
    """
    log.info("=" * 60)
    log.info("TARGET: global_lead_locks — Orphaned exclusivity lock flush")
    log.info("=" * 60)
    count = _batch_delete_collection("global_lead_locks")
    log.info(f"[global_lead_locks] COMPLETE. {count} locks removed.")
    return count


# ---------------------------------------------------------------------------
# Target 2: scraped_cache
# ---------------------------------------------------------------------------
def flush_scraped_cache() -> int:
    """
    Deletes all documents in the scraped_cache collection.

    scraped_cache holds intermediate Playwright scrape results keyed by URL.
    A prior bug caused stale/poisoned entries (empty text, mis-keyed documents)
    that caused dispatch() to serve cached garbage instead of re-scraping.

    Safe to flush entirely: the next dispatch() run will re-scrape all URLs
    and re-populate the cache with fresh data.
    """
    log.info("=" * 60)
    log.info("TARGET: scraped_cache — Poisoned scrape cache flush")
    log.info("=" * 60)
    count = _batch_delete_collection("scraped_cache")
    log.info(f"[scraped_cache] COMPLETE. {count} cache entries removed.")
    return count


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main():
    start = datetime.datetime.now(datetime.timezone.utc)
    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║  flush_stale_state.py — Sideio Platform State Migration  ║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    log.info(f"Project : {PROJECT_ID}")
    log.info(f"Dry run : {DRY_RUN}")
    log.info(f"Started : {start.isoformat()}")
    log.info(f"Batch sz: {BATCH_SIZE}")

    results = {}

    try:
        results["global_lead_locks"] = flush_global_lead_locks()
    except Exception as e:
        log.error(f"FATAL: global_lead_locks flush failed: {e}")
        sys.exit(1)

    try:
        results["scraped_cache"] = flush_scraped_cache()
    except Exception as e:
        log.error(f"FATAL: scraped_cache flush failed: {e}")
        sys.exit(1)

    elapsed = (datetime.datetime.now(datetime.timezone.utc) - start).total_seconds()

    log.info("=" * 60)
    log.info("MIGRATION COMPLETE")
    log.info(f"  global_lead_locks deleted : {results['global_lead_locks']}")
    log.info(f"  scraped_cache deleted     : {results['scraped_cache']}")
    log.info(f"  Total elapsed             : {elapsed:.1f}s")
    log.info("=" * 60)

    if DRY_RUN:
        log.info("DRY RUN — no data was modified. Re-run with DRY_RUN=false to execute.")


if __name__ == "__main__":
    main()
