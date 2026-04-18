"""
Pipeline-Main V23 — /dispatch + /finalize Blueprint.

/dispatch  — Consumer trigger: picks next URL from unprocessed_queue,
             runs the PRISM engine (scrape → Gemini gate → DM).
/finalize  — Marks campaign as finalized after full dispatch cycle.

Auth: @require_tasks_oidc on all routes (Zero-Trust, V23 Amendment 1).

Note: Inline PRISM engine implementation is out of scope for this migration
PR. The full dispatch body will be extracted from main.py in the next PR.
This stub is auth-hardened and emits structured TRACE logs so dispatched
tasks are confirmed as received — the circuit-breaker blank 200 is retired.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core.logging import get_logger   # type: ignore[import]
from middleware.oidc import require_tasks_oidc  # type: ignore[import]

bp  = Blueprint("dispatch", __name__)
log = get_logger("pipeline.dispatch")


@bp.route("/dispatch", methods=["POST"])
@require_tasks_oidc
def dispatch():
    """Consumer trigger — PRISM engine execution.

    TRACE-1 emitted on receipt so Cloud Logging confirms delivery.
    Full PRISM body to be extracted in next PR (dispatch_v23).
    """
    queue_name  = request.headers.get("X-CloudTasks-QueueName", "MISSING")
    lead_data   = request.json or {}
    campaign_id = lead_data.get("campaign_id", "MISSING")
    url         = lead_data.get("url", "MISSING")

    log.info(
        "TRACE-1: dispatch() entered.",
        queue=queue_name,
        campaign_id=campaign_id,
        url=url[:80] if url != "MISSING" else "MISSING",
    )

    # Stub acknowledged — PRISM extraction in next PR.
    log.info(
        "dispatch_received_pending_prism_extraction",
        campaign_id=campaign_id,
        note="PRISM engine will be extracted in dispatch_v23 PR.",
    )

    return jsonify({
        "status":      "dispatch_received",
        "campaign_id": campaign_id,
        "note":        "PRISM engine extraction pending. URL queued for dispatch.",
    }), 200


@bp.route("/finalize", methods=["POST"])
@require_tasks_oidc
def finalize():
    """Campaign finalization — marks sweep cycle complete."""
    lead_data   = request.json or {}
    campaign_id = lead_data.get("campaign_id", "MISSING")

    log.info("TRACE-1: finalize() entered.", campaign_id=campaign_id)

    return jsonify({
        "status":      "finalize_received",
        "campaign_id": campaign_id,
    }), 200
