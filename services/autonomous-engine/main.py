"""
main.py - V16 Autonomous Engine Cloud Run Job Entrypoint
=========================================================
Executes a multi-tenant sweep:
  - Queries users collection for all tenants with has_active_campaign == true
  - Runs TriangulationEngine for each tenant in isolation
  - One tenant error NEVER crashes the whole job (try/except per tenant)

Deployment: Cloud Run Job (not a Flask server)
Trigger:    Cloud Scheduler -> Cloud Run Jobs API (daily or configurable)
Auth:       Application Default Credentials on the Cloud Run SA
"""

import logging
import sys
import os

import firebase_admin
from firebase_admin import firestore as firebase_firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from engine import TriangulationEngine

# Structured logging to Cloud Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("autonomous-engine")


def main():
    # Init Firebase Admin SDK once
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firebase_firestore.client()

    log.info("[SWEEP] V16 Autonomous Engine starting multi-tenant sweep")

    # Query all tenants with active campaigns
    try:
        tenant_docs = (
            db.collection("users")
            .where(filter=FieldFilter("has_active_campaign", "==", True))
            .stream()
        )
        tenants = list(tenant_docs)
    except Exception as e:
        log.error(f"[SWEEP] Failed to query active tenants: {e}")
        sys.exit(1)

    if not tenants:
        log.info("[SWEEP] No active tenants found. Job complete.")
        return

    log.info(f"[SWEEP] Found {len(tenants)} active tenant(s)")

    total_written = 0
    for tenant_doc in tenants:
        tenant_id = tenant_doc.id
        try:
            log.info(f"[SWEEP] Processing tenant: {tenant_id}")
            engine       = TriangulationEngine(tenant_id=tenant_id, db=db)
            leads_written = engine.run()
            total_written += leads_written
            log.info(f"[SWEEP] Tenant {tenant_id} complete. Leads written: {leads_written}")
        except Exception as e:
            # Isolated: one bad tenant never kills the job
            log.error(f"[SWEEP] Tenant {tenant_id} failed with unhandled error: {e}", exc_info=True)
            continue

    log.info(f"[SWEEP] Multi-tenant sweep complete. Total leads written: {total_written}")


if __name__ == "__main__":
    main()
