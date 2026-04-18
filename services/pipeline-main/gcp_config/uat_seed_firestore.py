"""
V23 UAT — Firestore Test Tenant + Campaign Seed Script
=======================================================

Data isolation contract:
  - tenant_id   : tenant_sideio_internal_test
  - campaign_id : uat_campaign_v23_preview_001

This script creates the minimum Firestore documents required for the
manual Cloud Task trigger to execute the full /produce pipeline without
touching any paying customer's data, credits, or notification webhooks.

Usage:
    python gcp_config/uat_seed_firestore.py

Prerequisites:
    - gcloud auth application-default login   (or service account ADC)
    - pip install google-cloud-firestore
"""
from __future__ import annotations

import datetime
import sys

PROJECT_ID  = "trendpulse-app-2025"
TENANT_ID   = "tenant_sideio_internal_test"
CAMPAIGN_ID = "uat_campaign_v23_preview_001"


def seed(project_id: str = PROJECT_ID) -> None:
    from google.cloud import firestore  # type: ignore[import]
    db = firestore.Client(project=project_id)

    now_iso = datetime.datetime.utcnow().isoformat() + "Z"

    # ── 1. Tenant document ──────────────────────────────────────────────────
    db.collection("users").document(TENANT_ID).set({
        "email":             "uat-internal@sideio.com",
        "plan":              "internal_test",
        "credits_remaining": 9999,
        "uat_tenant":        True,
        "_created_at":       now_iso,
        "_note":             "UAT-only. No webhooks. No credit ledger. Do not route live traffic.",
    }, merge=True)
    print(f"[SEED] ✓ users/{TENANT_ID}")

    # ── 2. Campaign document (Persona Vault path) ───────────────────────────
    db.collection("campaigns").document(CAMPAIGN_ID).set({
        "id":               CAMPAIGN_ID,
        "tenant_id":        TENANT_ID,
        "name":             "V23 UAT Preview Campaign",
        # V23 Persona Vault fields (take precedence over legacy bio/keywords)
        "persona_id":       "persona_uat_001",
        "persona_name":     "UAT B2B SaaS Persona",
        "persona_bio":      (
            "We help SaaS companies reduce churn with AI-driven "
            "customer success automation."
        ),
        "persona_keywords": "SaaS, customer success, churn reduction, B2B, automation",
        # Legacy fallbacks (also populated so both paths are exercised)
        "bio":              "AI-driven customer success for SaaS.",
        "keywords":         "SaaS, churn, B2B",
        # Campaign metadata
        "sourcing_vector":  "Classic B2B",
        "location":         "India",
        "gl":               "in",
        "status":           "active",
        "unprocessed_queue": [],
        "last_produced_at": None,
        "_uat":             True,
        "_created_at":      now_iso,
    })
    print(f"[SEED] ✓ campaigns/{CAMPAIGN_ID}")

    # ── 3. Usage metrics baseline ────────────────────────────────────────────
    # Pre-create so firestore.Increment() doesn't fail on a missing document.
    db.collection("usage_metrics").document(TENANT_ID).set({
        "serper_searches": 0,
        "uat_run":         True,
    }, merge=True)
    print(f"[SEED] ✓ usage_metrics/{TENANT_ID}")

    print()
    print(f"  tenant_id   = {TENANT_ID}")
    print(f"  campaign_id = {CAMPAIGN_ID}")
    print()
    print("  Copy these into your Cloud Task payload:")
    print(f'  {{"tenant_id": "{TENANT_ID}", "campaign_id": "{CAMPAIGN_ID}"}}')


def verify(project_id: str = PROJECT_ID) -> None:
    """Print current state of seeded documents."""
    from google.cloud import firestore  # type: ignore[import]
    db = firestore.Client(project=project_id)

    camp = db.collection("campaigns").document(CAMPAIGN_ID).get().to_dict() or {}
    queue = camp.get("unprocessed_queue", [])
    produced = camp.get("last_produced_at")

    print(f"\n[VERIFY] campaigns/{CAMPAIGN_ID}")
    print(f"  unprocessed_queue depth : {len(queue)}")
    print(f"  last_produced_at        : {produced}")
    if queue:
        print("  First 5 URLs:")
        for u in queue[:5]:
            print(f"    {u}")

    if len(queue) > 0 and produced:
        print("\n  [UAT GATE 5] PASS: Producer wrote URLs to queue. ✅")
        return 0
    else:
        print("\n  [UAT GATE 5] FAIL: Queue empty or last_produced_at missing. ❌")
        return 1


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "seed"
    if action == "seed":
        seed()
    elif action == "verify":
        sys.exit(verify())
    else:
        print(f"Usage: python uat_seed_firestore.py [seed|verify]")
        sys.exit(1)
