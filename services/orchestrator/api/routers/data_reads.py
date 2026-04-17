"""
Orchestrator — /api/campaigns, /api/leads, /api/tenant_profiles Blueprint.

Routes (GET):
  GET /api/campaigns       — List campaigns for tenant
  GET /api/leads           — List leads (with optional ?crm= filter)
  GET /api/tenant_profiles — Fetch Master Twin

All DB access via ``repositories.firestore_repo``.  No business logic here.
"""
from __future__ import annotations

import datetime
from typing import Any

from flask import Blueprint, jsonify, request
from google.cloud import firestore as fs

from api.middleware import require_auth
from core.clients import get_db
from core.logging import get_logger
from repositories.firestore_repo import (
    list_campaigns,
    list_leads,
)

log = get_logger(__name__)

bp = Blueprint("data_reads", __name__)


def _sanitize(doc) -> dict[str, Any]:
    """Convert a Firestore DocumentSnapshot to a JSON-safe dict.

    Strips Firestore Timestamp objects to ISO strings and injects the document ID.

    Args:
        doc: Firestore DocumentSnapshot.

    Returns:
        JSON-serializable dict.
    """
    data = doc.to_dict() or {}
    data["id"] = doc.id
    for k, v in data.items():
        if hasattr(v, "isoformat"):
            data[k] = v.isoformat()
    return data


@bp.route("/api/campaigns", methods=["GET", "OPTIONS"])
@require_auth
def get_campaigns(uid: str, tenant_id: str, user_role: str):
    """List all campaigns for the authenticated tenant.

    Returns:
        JSON with ``status`` and ``data`` (list of campaign dicts).
    """
    campaigns = list_campaigns(get_db(), tenant_id)
    log.info("campaigns_listed", tenant=tenant_id[:8], count=len(campaigns))
    return jsonify({"status": "success", "data": campaigns}), 200


@bp.route("/api/leads", methods=["GET", "OPTIONS"])
@require_auth
def get_leads(uid: str, tenant_id: str, user_role: str):
    """List leads for the authenticated tenant.

    Query params:
        crm (str): ``"true"`` for CRM board only, ``"false"`` for dashboard feed.

    Returns:
        JSON with ``status`` and ``data`` (list of lead dicts).
    """
    crm_param = request.args.get("crm")
    crm_filter = None
    if crm_param == "true":
        crm_filter = True
    elif crm_param == "false":
        crm_filter = False

    leads = list_leads(get_db(), tenant_id, crm_filter=crm_filter)
    log.info("leads_listed", tenant=tenant_id[:8], count=len(leads), crm_filter=crm_filter)
    return jsonify({"status": "success", "data": leads}), 200


@bp.route("/api/tenant_profiles", methods=["GET", "OPTIONS"])
@require_auth
def get_tenant_profiles(uid: str, tenant_id: str, user_role: str):
    """Fetch the Master Digital Twin profile for the authenticated tenant.

    Returns:
        JSON with ``status`` and ``data`` (list with one profile dict, or empty).
    """
    db = get_db()
    doc = db.collection("tenant_profiles").document(tenant_id).get()
    data = []
    if doc.exists:
        d = doc.to_dict() or {}
        d["id"] = doc.id
        data = [d]
    log.info("tenant_profile_fetched", tenant=tenant_id[:8], found=bool(data))
    return jsonify({"status": "success", "data": data}), 200
