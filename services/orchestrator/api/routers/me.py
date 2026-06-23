"""
Orchestrator — /api/me Blueprint.

Routes:
  GET /api/me  — Fetch user profile + combined wallet
  PUT /api/me  — Update agreed_to_terms / crm_webhook_url
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from google.cloud import firestore as fs

from api.middleware import require_auth
from core.clients import get_db
from core.logging import get_logger
from repositories.firestore_repo import get_wallet_shards_total

log = get_logger(__name__)

bp = Blueprint("me", __name__)


@bp.route("/api/me", methods=["GET", "PUT", "OPTIONS"])
@require_auth
def me_endpoint(uid: str, tenant_id: str, user_role: str):
    """Fetch or update the current user's profile.

    GET:
      Returns user data dict plus a normalised ``wallet``
      (``allocated_credits`` + ``consumed_credits`` including shard sum).

    PUT body (JSON):
        agreed_to_terms (bool): Stamps server timestamp when truthy.
        crm_webhook_url (str):  Persists CRM webhook URL.

    Returns:
        JSON with ``status``, ``data`` (GET only), ``wallet`` (GET only).
    """
    db = get_db()

    if request.method == "PUT":
        payload = request.json or {}
        updates: dict = {}
        if "agreed_to_terms" in payload:
            updates["agreed_to_terms"] = fs.SERVER_TIMESTAMP
        if "crm_webhook_url" in payload:
            crm_url = payload["crm_webhook_url"]
            if crm_url and not crm_url.startswith(("http://", "https://")):
                return jsonify({"error": "Webhook URL must start with http:// or https://"}), 400
            updates["crm_webhook_url"] = crm_url
        # V23.5: inbound radar toggle
        if "inbound_radar_enabled" in payload:
            updates["inbound_radar.enabled"] = bool(payload["inbound_radar_enabled"])
        if updates:
            db.collection("users").document(uid).update(updates)
            log.info("user_profile_updated", uid=uid[:8], fields=list(updates.keys()))
        return jsonify({"status": "success", "message": "Updated."}), 200

    # GET
    user_doc = db.collection("users").document(uid).get()
    if not user_doc.exists:
        return jsonify({"error": "User structure missing"}), 404

    data = user_doc.to_dict() or {}
    raw_wallet = data.get("wallet", {})
    allocated = int(raw_wallet.get("allocated_credits", 0) or 0)
    consumed = int(raw_wallet.get("consumed_credits", 0) or 0)
    consumed += get_wallet_shards_total(db, uid)

    log.info("user_profile_fetched", uid=uid[:8])

    # Inbound radar status — safe summary only (no keys, no raw config)
    radar_raw = data.get("inbound_radar") or {}
    inbound_radar_summary = {
        "enabled":           radar_raw.get("enabled", False),
        "last_ran_at":       str(radar_raw.get("last_ran_at") or ""),
        "signals_this_week": int(radar_raw.get("signals_this_week") or 0),
        "top_pain_keywords": radar_raw.get("top_pain_keywords") or [],
    }

    return jsonify({
        "status":        "success",
        "data":          data,
        "wallet":        {"allocated_credits": allocated, "consumed_credits": consumed},
        "inbound_radar": inbound_radar_summary,
    }), 200
