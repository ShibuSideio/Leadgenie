"""
Pipeline-Main V25.2.0 — /harvest Blueprint

THE DEDICATED HARVESTER — 4-Hour Signal Collection.
====================================================
Runs signal_harvest.harvest_signals() for a single campaign independently
of the Serper /produce sweep. Called by Cloud Scheduler every 4 hours via
Orchestrator /cron/harvest-sweep fan-out.

Key difference from /produce:
  - /produce runs QueryBrain Serper AND signal harvest with allow_serper=True
  - /harvest runs ONLY free (non-Serper) signal sources: Reddit RSS, HN,
    RSS feeds, classifieds, consumer forums, job posts, YouTube, etc.
  - SerperDiscovery, Google Reviews (Maps+Reviews), and Reddit Serper
    fallback are HARD-BLOCKED here (allow_serper=False). No API key is
    loaded. Automatic harvest must never burn Serper credits.

Auth: @require_tasks_oidc (same as /produce and /dispatch)
Payload: { "tenant_id": str, "campaign_id": str }
"""
from __future__ import annotations

import datetime

from flask import Blueprint, jsonify, request

from core.clients import get_db                                           # type: ignore[import]
from core.logging import get_logger                                       # type: ignore[import]
from middleware.oidc import require_tasks_oidc                            # type: ignore[import]
from services.signal_harvest import harvest_signals                       # type: ignore[import]

bp  = Blueprint("harvest", __name__)
log = get_logger("pipeline.harvest")


def _db():
    return get_db()


@bp.route("/harvest", methods=["POST"])
@require_tasks_oidc
def harvest():
    """V25.2.0 — Free-source signal harvest endpoint (4-hour cadence).

    Runs signal_harvest.harvest_signals() for a single campaign with
    allow_serper=False. Writes free-source signals to scraped_cache +
    unprocessed_queue. Does NOT load a Serper key and does NOT run
    SerperDiscovery / Google Reviews / Reddit Serper fallback.

    Request body: {"tenant_id": "...", "campaign_id": "..."}
    Returns: harvest metrics dict
    """
    body        = request.get_json(silent=True) or {}
    tenant_id   = body.get("tenant_id", "").strip()
    campaign_id = body.get("campaign_id", "").strip()

    if not tenant_id or not campaign_id:
        log.warning("harvest_missing_params", body=str(body)[:200])
        return jsonify({"error": "tenant_id and campaign_id are required"}), 400

    # Fetch campaign from Firestore
    try:
        doc = _db().collection("campaigns").document(campaign_id).get()
        if not doc.exists:
            log.warning("harvest_campaign_not_found", campaign_id=campaign_id)
            return jsonify({"error": "Campaign not found"}), 404
        campaign = doc.to_dict() or {}
        campaign["id"] = campaign_id
    except Exception as exc:
        log.error("harvest_campaign_fetch_failed", campaign_id=campaign_id, error=str(exc))
        return jsonify({"error": "Failed to fetch campaign"}), 500

    # Tenant isolation — campaign.tenant_id must match request tenant_id
    if campaign.get("tenant_id") != tenant_id:
        log.warning(
            "harvest_tenant_mismatch",
            campaign_id=campaign_id,
            request_tenant=tenant_id,
            campaign_tenant=campaign.get("tenant_id"),
        )
        return jsonify({"error": "Tenant isolation violation"}), 403

    # Skip inactive campaigns
    if campaign.get("status") not in ("active", "running"):
        log.info(
            "harvest_campaign_inactive",
            campaign_id=campaign_id,
            status=campaign.get("status"),
        )
        return jsonify({"message": "Campaign is not active — harvest skipped", "skipped": True}), 200

    # Run free-source harvest only — never load Serper credentials here.
    log.info(
        "harvest_start",
        campaign_id=campaign_id,
        tenant_id=tenant_id,
        archetype=campaign.get("sourcing_vector", "B2B"),
        allow_serper=False,
        note="Free sources only. Serper blocked on automatic harvest path.",
    )

    try:
        metrics = harvest_signals(
            campaign       = campaign,
            db             = _db(),
            serper_api_key = "",
            allow_serper   = False,
        )
    except Exception as exc:
        log.error(
            "harvest_failed",
            campaign_id=campaign_id,
            error=str(exc),
            exc_info=True,
        )
        return jsonify({"error": "Harvest pipeline failed", "detail": str(exc)}), 500

    log.info(
        "harvest_complete",
        campaign_id=campaign_id,
        **metrics,
    )

    # Trigger cluster analysis if enough signals were harvested
    if metrics.get("queued", 0) >= 1 or metrics.get("scored", 0) >= 3:
        try:
            from services.context_builder import build_enriched_context  # type: ignore[import]
            from services.signal_cluster_analyst import analyse_and_create_leads

            icp_context = build_enriched_context(campaign)
            cluster_result = analyse_and_create_leads(
                campaign_id = campaign_id,
                tenant_id   = tenant_id,
                icp_context = icp_context,
                archetype   = campaign.get("sourcing_vector", "B2B"),
                geo         = campaign.get("location", ""),
                db          = _db(),
                campaign    = campaign,
            )
            log.info("harvest_cluster_analysis_done", campaign_id=campaign_id, **cluster_result)
            metrics["cluster_result"] = cluster_result
        except Exception as cluster_exc:
            log.warning("harvest_cluster_analysis_failed", campaign_id=campaign_id, error=str(cluster_exc))

    return jsonify({
        "campaign_id": campaign_id,
        "tenant_id":   tenant_id,
        "metrics":     metrics,
        "allow_serper": False,
        "harvested_at": datetime.datetime.utcnow().isoformat() + "Z",
    }), 200
