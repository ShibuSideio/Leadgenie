"""
Pipeline-Main V25.2.0 — /harvest Blueprint

THE DEDICATED HARVESTER — 4-Hour Signal Collection.
====================================================
Runs signal_harvest.harvest_signals() for a single campaign independently
of the Serper /produce sweep. Called by Cloud Scheduler every 4 hours via
Orchestrator /cron/harvest-sweep fan-out.

Key difference from /produce:
  - /produce runs BOTH Serper (QueryBrain) AND signal harvest
  - /harvest runs ONLY signal harvest (no Serper, no QueryBrain)
  - This allows fresh signals to surface between 6-hour Serper sweeps

Auth: @require_tasks_oidc (same as /produce and /dispatch)
Payload: { "tenant_id": str, "campaign_id": str }
"""
from __future__ import annotations

import datetime
import os

from flask import Blueprint, jsonify, request

from core.clients import get_db, get_sm_client                       # type: ignore[import]
from core.config import SERPER_API_KEY_NAME                           # type: ignore[import]
from core.logging import get_logger                                   # type: ignore[import]
from middleware.oidc import require_tasks_oidc                        # type: ignore[import]
from services.signal_harvest import harvest_signals                   # type: ignore[import]

bp  = Blueprint("harvest", __name__)
log = get_logger("pipeline.harvest")


def _db():
    return get_db()


def _get_serper_key() -> str:
    client   = get_sm_client()
    response = client.access_secret_version(request={"name": SERPER_API_KEY_NAME})
    return response.payload.data.decode("utf-8")


@bp.route("/harvest", methods=["POST"])
@require_tasks_oidc
def harvest():
    """V25.2.0 — Signal harvest endpoint (4-hour cadence).

    Runs signal_harvest.harvest_signals() for a single campaign.
    Writes signals to scraped_cache + unprocessed_queue (same as /produce).
    Does NOT run Serper/QueryBrain — that is /produce's responsibility.

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

    # Run harvest
    log.info(
        "harvest_start",
        campaign_id=campaign_id,
        tenant_id=tenant_id,
        archetype=campaign.get("sourcing_vector", "B2B"),
    )

    try:
        serper_key = _get_serper_key()
    except Exception as exc:
        log.warning("harvest_serper_key_failed", error=str(exc), note="Proceeding without SerperDiscoverySource.")
        serper_key = ""

    try:
        metrics = harvest_signals(
            campaign      = campaign,
            db            = _db(),
            serper_api_key= serper_key,
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
            )
            log.info("harvest_cluster_analysis_done", campaign_id=campaign_id, **cluster_result)
            metrics["cluster_result"] = cluster_result
        except Exception as cluster_exc:
            log.warning("harvest_cluster_analysis_failed", campaign_id=campaign_id, error=str(cluster_exc))

    return jsonify({
        "campaign_id": campaign_id,
        "tenant_id":   tenant_id,
        "metrics":     metrics,
        "harvested_at": datetime.datetime.utcnow().isoformat() + "Z",
    }), 200
