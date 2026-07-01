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
        crm      (str): ``"true"`` for CRM board only, ``"false"`` for dashboard feed.
        sort_by  (str): Field to sort by — ``"score"`` or ``"createdAt"`` (default).
        sort_dir (str): ``"asc"`` or ``"desc"`` (default).
        min_score (int): Minimum normalized_score threshold (0–100); 0 means no filter.

    Returns:
        JSON with ``status`` and ``data`` (list of lead dicts).
    """
    from google.cloud.firestore_v1.base_query import FieldFilter as _FF
    from google.cloud.firestore_v1 import Query as _Query

    crm_param = request.args.get("crm")
    crm_filter = None
    if crm_param == "true":
        crm_filter = True
    elif crm_param == "false":
        crm_filter = False

    # V24.4 (L5-5): Optional sort_by and min_score query parameters.
    sort_by   = request.args.get("sort_by", "createdAt")  # default: chronological
    min_score = request.args.get("min_score", type=int, default=0)
    sort_dir  = request.args.get("sort_dir", "desc").lower()  # asc or desc

    db = get_db()
    q = db.collection("leads").where(filter=_FF("tenant_id", "==", tenant_id))
    if crm_filter is not None:
        q = q.where(filter=_FF("is_in_crm", "==", crm_filter))

    if min_score > 0:
        q = q.where(filter=_FF("normalized_score", ">=", min_score * 10))
    # Sort field
    if sort_by == "score":
        _sort_direction = _Query.DESCENDING if sort_dir != "asc" else _Query.ASCENDING
        q = q.order_by("normalized_score", direction=_sort_direction)
    else:
        q = q.order_by("createdAt", direction=_Query.DESCENDING)

    docs = q.limit(200).stream()
    leads = []
    for doc in docs:
        d = doc.to_dict() or {}
        d["id"] = doc.id
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        leads.append(d)

    log.info("leads_listed", tenant=tenant_id[:8], count=len(leads),
             crm_filter=crm_filter, sort_by=sort_by, min_score=min_score)
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
