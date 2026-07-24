"""
Orchestrator — /api/campaigns, /api/leads, /api/tenant_profiles Blueprint.

Routes (GET):
  GET /api/campaigns       — List campaigns for tenant
  GET /api/leads           — List leads (with optional ?crm= filter)
  GET /api/leads/export    — Export leads (all fields; json|csv; campaign/status filters)
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


def _json_safe_lead(doc) -> dict[str, Any]:
    """Full lead document as JSON-safe dict (all fields)."""
    data = doc.to_dict() or {}
    data["id"] = doc.id
    return _json_safe_value(data)


def _json_safe_value(val: Any) -> Any:
    if hasattr(val, "isoformat"):
        try:
            return val.isoformat()
        except Exception:
            return str(val)
    if isinstance(val, dict):
        return {str(k): _json_safe_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_json_safe_value(x) for x in val]
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    # Firestore GeoPoint / sentinels
    if type(val).__name__ in ("GeoPoint", "Sentinel"):
        return str(val)
    return val


def _lead_matches_campaign(lead: dict, campaign_id: str) -> bool:
    if not campaign_id:
        return True
    if str(lead.get("campaign_id") or "") == campaign_id:
        return True
    if str(lead.get("highest_campaign_id") or "") == campaign_id:
        return True
    matched = lead.get("matched_campaigns") or lead.get("matched_campaign_ids") or []
    if isinstance(matched, list) and campaign_id in [str(x) for x in matched]:
        return True
    return False


def _flatten_for_csv(row: dict) -> dict[str, str]:
    """Flatten nested structures to strings for CSV export."""
    import json as _json
    flat: dict[str, str] = {}
    for k, v in row.items():
        if v is None:
            flat[str(k)] = ""
        elif isinstance(v, (dict, list)):
            flat[str(k)] = _json.dumps(v, ensure_ascii=False, default=str)
        else:
            flat[str(k)] = str(v)
    return flat


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
        # V24.5.5 FIX: normalized_score is stored as 0-100 in Firestore (score × 10).
        # Do NOT multiply by 10 again — that would treat min_score=5 as >=50, blocking
        # all leads below 50/100 and making the score filter 10× too aggressive.
        q = q.where(filter=_FF("normalized_score", ">=", min_score))
    # V24.5.5 FIX: Only return completed, qualified leads (status='new').
    # The feed previously returned ALL statuses including zombie 'processing' stubs,
    # 'enrichment_pending' parking stubs, and 'failed' error stubs — cluttering the
    # UI and making users think they had more/fewer leads than they actually do.
    q = q.where(filter=_FF("status", "==", "new"))
    # Sort field
    if sort_by == "score":
        _sort_direction = _Query.DESCENDING if sort_dir != "asc" else _Query.ASCENDING
        q = q.order_by("normalized_score", direction=_sort_direction)
    else:
        q = q.order_by("createdAt", direction=_Query.DESCENDING)

    docs = q.limit(200).stream()
    leads = []
    for doc in docs:
        leads.append(_json_safe_lead(doc))

    log.info("leads_listed", tenant=tenant_id[:8], count=len(leads),
             crm_filter=crm_filter, sort_by=sort_by, min_score=min_score)
    return jsonify({"status": "success", "data": leads}), 200


@bp.route("/api/leads/export", methods=["GET", "OPTIONS"])
@require_auth
def export_leads(uid: str, tenant_id: str, user_role: str):
    """Export leads for the tenant with **all** Firestore fields.

    Query params:
        campaign_id (str): Optional. Filter by campaign_id / matched_campaigns.
        status      (str): ``new`` (default), ``all``, or a specific status.
        format      (str): ``json`` (default) or ``csv``.
        limit       (int): Max rows (default 2000, max 5000).

    Returns:
        JSON body or CSV download with Content-Disposition attachment.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter as _FF
    from flask import Response
    import csv
    import io
    import json as _json

    campaign_id = (request.args.get("campaign_id") or "").strip()
    status_param = (request.args.get("status") or "new").strip().lower()
    fmt = (request.args.get("format") or "json").strip().lower()
    try:
        limit = int(request.args.get("limit") or 2000)
    except (TypeError, ValueError):
        limit = 2000
    limit = max(1, min(limit, 5000))

    db = get_db()
    by_id: dict[str, dict] = {}

    def _ingest(stream, cap: int) -> None:
        for doc in stream:
            if len(by_id) >= cap:
                break
            lead = _json_safe_lead(doc)
            if campaign_id and not _lead_matches_campaign(lead, campaign_id):
                continue
            by_id[doc.id] = lead

    # Dual-path campaign filter when campaign_id set (identity SSOT dual fields)
    if campaign_id:
        q_a = (
            db.collection("leads")
            .where(filter=_FF("tenant_id", "==", tenant_id))
            .where(filter=_FF("campaign_id", "==", campaign_id))
            .limit(limit)
        )
        if status_param and status_param != "all":
            q_a = (
                db.collection("leads")
                .where(filter=_FF("tenant_id", "==", tenant_id))
                .where(filter=_FF("campaign_id", "==", campaign_id))
                .where(filter=_FF("status", "==", status_param))
                .limit(limit)
            )
        try:
            _ingest(q_a.stream(), limit)
        except Exception as exc:
            log.warning("leads_export_q_campaign_id_failed", error=str(exc))
            # Fallback: tenant stream + client filter
            q_fb = db.collection("leads").where(filter=_FF("tenant_id", "==", tenant_id)).limit(min(limit * 3, 5000))
            if status_param and status_param != "all":
                q_fb = (
                    db.collection("leads")
                    .where(filter=_FF("tenant_id", "==", tenant_id))
                    .where(filter=_FF("status", "==", status_param))
                    .limit(min(limit * 3, 5000))
                )
            _ingest(q_fb.stream(), limit)

        if len(by_id) < limit:
            try:
                q_b = (
                    db.collection("leads")
                    .where(filter=_FF("tenant_id", "==", tenant_id))
                    .where(filter=_FF("matched_campaigns", "array_contains", campaign_id))
                    .limit(limit)
                )
                if status_param and status_param != "all":
                    # array_contains + status may need index; try and fall back
                    try:
                        q_b = (
                            db.collection("leads")
                            .where(filter=_FF("tenant_id", "==", tenant_id))
                            .where(filter=_FF("matched_campaigns", "array_contains", campaign_id))
                            .where(filter=_FF("status", "==", status_param))
                            .limit(limit)
                        )
                        _ingest(q_b.stream(), limit)
                    except Exception:
                        q_b = (
                            db.collection("leads")
                            .where(filter=_FF("tenant_id", "==", tenant_id))
                            .where(filter=_FF("matched_campaigns", "array_contains", campaign_id))
                            .limit(limit)
                        )
                        for doc in q_b.stream():
                            if len(by_id) >= limit:
                                break
                            lead = _json_safe_lead(doc)
                            if status_param != "all" and str(lead.get("status") or "").lower() != status_param:
                                continue
                            by_id[doc.id] = lead
                else:
                    _ingest(q_b.stream(), limit)
            except Exception as exc:
                log.warning("leads_export_q_matched_campaigns_failed", error=str(exc))
    else:
        q = db.collection("leads").where(filter=_FF("tenant_id", "==", tenant_id))
        if status_param and status_param != "all":
            q = q.where(filter=_FF("status", "==", status_param))
        q = q.limit(limit)
        try:
            _ingest(q.stream(), limit)
        except Exception as exc:
            log.error("leads_export_query_failed", error=str(exc))
            return jsonify({"error": "Export query failed", "details": str(exc)}), 500

    leads = list(by_id.values())
    # Stable sort: newest first when createdAt present
    def _sort_key(row: dict):
        return str(row.get("createdAt") or row.get("created_at") or "")

    leads.sort(key=_sort_key, reverse=True)

    log.info(
        "leads_exported",
        tenant=tenant_id[:8],
        count=len(leads),
        campaign_id=campaign_id or "all",
        status=status_param,
        format=fmt,
    )

    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    camp_slug = (campaign_id or "all-campaigns")[:24]
    base_name = f"sideio-leads-{camp_slug}-{stamp}"

    if fmt == "csv":
        flat_rows = [_flatten_for_csv(r) for r in leads]
        # Union of all keys so no field is dropped
        fieldnames: list[str] = []
        seen_f: set[str] = set()
        for row in flat_rows:
            for k in row.keys():
                if k not in seen_f:
                    seen_f.add(k)
                    fieldnames.append(k)
        if "id" in fieldnames:
            fieldnames.remove("id")
            fieldnames.insert(0, "id")
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames or ["id"], extrasaction="ignore")
        writer.writeheader()
        for row in flat_rows:
            writer.writerow(row)
        # UTF-8 BOM so Excel opens non-ASCII columns correctly
        body = "\ufeff" + buf.getvalue()
        return Response(
            body,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{base_name}.csv"',
                "X-Export-Count": str(len(leads)),
            },
        )

    payload = {
        "status": "success",
        "exported_at": datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "campaign_id": campaign_id or None,
        "status_filter": status_param,
        "count": len(leads),
        "leads": leads,
    }
    body = _json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{base_name}.json"',
            "X-Export-Count": str(len(leads)),
        },
    )


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
