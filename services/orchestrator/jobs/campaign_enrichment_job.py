"""
Orchestrator — Campaign Enrichment Backfill Job.

Self-heals active campaigns that predate the auto-enrichment layer or still
have weak operational fields. Safe to run repeatedly: only updates campaigns
that are missing enrichment, use stale enrichment versions, or show clear
quality risk signals.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ORCH_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, _ORCH_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from google.cloud import firestore  # noqa: E402

from core.clients import get_db  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]
from shared.campaign_enrichment import derive_campaign_enrichment  # type: ignore[import]

log = get_logger("orchestrator.campaign_enrichment_job")

_BATCH_SIZE = int(os.environ.get("CAMPAIGN_ENRICHMENT_BATCH_SIZE", "200"))
_CURRENT_VERSION = "2026-07-15-auto-enrichment-v1"


def _is_broad_location(location: str) -> bool:
    lowered = (location or "").strip().lower()
    if not lowered:
        return True
    return lowered.startswith("all,") or lowered in {"all", "global", "worldwide"}


def _campaign_needs_enrichment(campaign: dict) -> bool:
    system_enrichment = campaign.get("system_enrichment") or {}
    if not isinstance(system_enrichment, dict):
        system_enrichment = {}

    if system_enrichment.get("enrichment_version") != _CURRENT_VERSION:
        return True
    if not campaign.get("persona_keywords"):
        return True
    if not campaign.get("persona_targeting_signals"):
        return True
    if not campaign.get("target_angle_hook"):
        return True
    if not campaign.get("unfair_advantage"):
        return True
    if _is_broad_location(str(campaign.get("location") or "")):
        return True

    strategy = campaign.get("intelligence_strategy") or {}
    if not isinstance(strategy, dict):
        strategy = {}
    platform_targets = strategy.get("platform_targets") or []
    if not platform_targets:
        return True
    if any("." not in str(target) for target in platform_targets):
        return True

    exhaustion_zeros = int(campaign.get("_query_exhaustion_consecutive_zeros") or 0)
    if exhaustion_zeros > 0:
        return True

    return False


def run() -> dict:
    """Backfill and refresh campaign enrichment for active campaigns."""
    db = get_db()
    processed = 0
    enriched = 0
    skipped = 0
    errors = 0

    docs = (
        db.collection("campaigns")
        .where(filter=firestore.FieldFilter("status", "==", "active"))
        .limit(_BATCH_SIZE)
        .stream()
    )

    for snap in docs:
        processed += 1
        campaign = snap.to_dict() or {}
        campaign["id"] = snap.id

        if not _campaign_needs_enrichment(campaign):
            skipped += 1
            continue

        try:
            updates = derive_campaign_enrichment(campaign)
            if not updates:
                skipped += 1
                continue
            updates["updatedAt"] = firestore.SERVER_TIMESTAMP
            snap.reference.update(updates)
            enriched += 1
        except Exception as exc:
            errors += 1
            log.error("campaign_enrichment_job_doc_failed", campaign_id=snap.id, error=str(exc))

    result = {
        "processed": processed,
        "enriched": enriched,
        "skipped": skipped,
        "errors": errors,
        "batch_size": _BATCH_SIZE,
    }
    log.info("campaign_enrichment_job_complete", **result)
    return result


if __name__ == "__main__":
    print(run())
