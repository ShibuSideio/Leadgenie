"""
Orchestrator — /api/analytics Blueprint.

Routes:
  GET  /api/analytics/roi                — L1 ROI dashboard
  PUT  /api/analytics/unit-economics     — Persist tenant unit_economics

All business logic delegated to ``services.analytics_service``.
Route handlers own: request parsing, auth, HTTP response shaping only.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from api.middleware import require_auth
from core.clients import get_db
from core.logging import get_logger
from services.analytics_service import compute_roi_metrics, validate_and_build_ue_update
from core.exceptions import ValidationError

log = get_logger(__name__)

bp = Blueprint("analytics", __name__)


@bp.route("/api/analytics/roi", methods=["GET", "OPTIONS"])
@require_auth
def get_roi_analytics(uid: str, tenant_id: str, user_role: str):
    """Return computed ROI metrics for the tenant's date window.

    Query params:
        date_range (int): Lookback days, 1-365.  Default: 30.

    Returns:
        JSON with ``status``, ``date_range_days``, ``unit_economics``,
        ``metrics``, and ``generated_at``.
    """
    try:
        date_range_days = max(1, min(int(request.args.get("date_range", 30)), 365))
    except (ValueError, TypeError):
        date_range_days = 30

    result = compute_roi_metrics(get_db(), tenant_id, date_range_days)
    log.info("roi_request_served", tenant=tenant_id[:8], days=date_range_days)
    return jsonify({"status": "success", **result}), 200


@bp.route("/api/analytics/unit-economics", methods=["PUT", "OPTIONS"])
@require_auth
def update_unit_economics(uid: str, tenant_id: str, user_role: str):
    """Persist custom unit_economics into the tenant's user document.

    Request body (JSON):
        avg_cpl (float): Cost per lead in USD.
        avg_deal_size (float): Average deal size.
        sdr_hourly_rate (float): SDR hourly rate.
        est_conversion_rate (float): Lead-to-close conversion rate (0-1).
        currency (str): 3-letter ISO currency code.

    Returns:
        JSON with ``status`` and ``message``.
    """
    payload = request.json or {}
    try:
        updates = validate_and_build_ue_update(payload)
    except ValidationError as exc:
        return jsonify({"error": exc.message}), 400

    get_db().collection("users").document(tenant_id).set(updates, merge=True)
    log.info("unit_economics_updated", tenant=tenant_id[:8], fields=list(updates.keys()))
    return jsonify({"status": "success", "message": "Unit economics saved."}), 200
