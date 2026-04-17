"""
Orchestrator V23 — L0 Governance Blueprint.

All routes require super_admin role.

Routes:
  GET  /api/l0/telemetry
  GET  /api/l0/trends
  GET  /api/l0/users
  POST /api/l0/users/suspend
  POST /api/l0/users/<uid>/mint
  POST /api/l0/users/<uid>/approve
  GET  /api/l0/system-health
  GET  /api/l0/shadow-ledger
  GET  /api/internal/l0/operations-telemetry
"""
from __future__ import annotations

import datetime

from flask import Blueprint, jsonify, request
from google.cloud.firestore_v1.base_query import FieldFilter

from core.clients import get_db  # type: ignore[import]
from core.config import PROJECT_ID  # type: ignore[import]
from core.auth import require_auth, require_super_admin  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]

db = get_db()

bp = Blueprint("l0_admin", __name__)
log = get_logger("orchestrator.v23.l0_admin")

# In-memory TTLCache reused from startup (imported from config)
try:
    from cachetools import TTLCache
    _OPS_CACHE_TTL = 300
    _ops_cache = TTLCache(maxsize=4, ttl=_OPS_CACHE_TTL)
except ImportError:
    _ops_cache = {}
    _OPS_CACHE_TTL = 300


def _sanitize(doc) -> dict:
    data = doc.to_dict() or {}
    data["id"] = doc.id
    for k, v in data.items():
        if hasattr(v, "timestamp"):
            data[k] = v.isoformat()
    return data


# =============================================================================
# GET /api/l0/telemetry
# =============================================================================
@bp.route("/api/l0/telemetry", methods=["GET"])
@require_auth
@require_super_admin
def get_l0_telemetry(uid, tenant_id, user_role):
    from google.cloud import firestore  # noqa: F401 (used for Query)
    macro_totals: dict = {}
    for st in ["new", "contacted", "ignored", "failed", "processing", "completed"]:
        res = db.collection("leads").where(field_path="status", op_string="==", value=st).count().get()
        macro_totals[st] = res[0][0].value
    macro_totals["total_leads"] = sum(macro_totals.values())

    tenants = []
    for user_doc in db.collection("users").stream():
        u_data = user_doc.to_dict()
        t_id   = u_data.get("tenant_id", user_doc.id)
        leads_count = db.collection("leads").where(field_path="tenant_id", op_string="==", value=t_id).count().get()[0][0].value
        wallet      = u_data.get("wallet", {})
        shard_sum   = sum(
            s.to_dict().get("consumed_credits", 0)
            for s in db.collection("users").document(t_id).collection("wallet_shards").stream()
        )
        t_info = u_data.copy()
        t_info.update({"tenant_id": t_id,
                        "wallet_balance": wallet.get("allocated_credits", 0) - wallet.get("consumed_credits", 0) - shard_sum,
                        "total_leads_generated": leads_count})
        tenants.append(t_info)

    return jsonify({"status": "success", "data": {
        "macro": macro_totals,
        "tenants": sorted(tenants, key=lambda x: x.get("total_leads_generated", 0), reverse=True),
    }}), 200


# =============================================================================
# GET /api/l0/trends
# =============================================================================
@bp.route("/api/l0/trends", methods=["GET"])
@require_auth
@require_super_admin
def get_l0_trends(uid, tenant_id, user_role):
    users_stream = db.collection("users").stream()
    user_map = {u.id: u.to_dict().get("email", "Unknown") for u in users_stream}
    trends = []
    for camp in db.collection("campaigns").stream():
        c = camp.to_dict()
        t_id = c.get("tenant_id")
        if not t_id or c.get("status", "paused") != "active":
            continue
        leads_count = db.collection("leads").where(field_path="campaign_id", op_string="==", value=camp.id).count().get()[0][0].value
        trends.append({
            "campaign_id": camp.id, "tenant_id": t_id,
            "email": user_map.get(t_id, "Unknown"),
            "name": c.get("name", ""), "bio": c.get("bio", ""),
            "keywords": c.get("keywords", ""), "leads_generated": leads_count,
        })
    return jsonify({"status": "success", "data": {
        "campaign_trends": sorted(trends, key=lambda x: x["leads_generated"], reverse=True)
    }}), 200


# =============================================================================
# GET /api/l0/users
# =============================================================================
@bp.route("/api/l0/users", methods=["GET"])
@require_auth
@require_super_admin
def get_l0_users(uid, tenant_id, user_role):
    docs = db.collection("users").limit(100).stream()
    results = [_sanitize(doc) for doc in docs]
    for res in results:
        usage_doc = db.collection("usage_metrics").document(res.get("tenant_id", "")).get()
        res["usage_metrics"] = usage_doc.to_dict() if usage_doc.exists else {}
    return jsonify({"status": "success", "data": results}), 200


# =============================================================================
# POST /api/l0/users/suspend
# =============================================================================
@bp.route("/api/l0/users/suspend", methods=["POST"])
@require_auth
@require_super_admin
def suspend_user(uid, tenant_id, user_role):
    data         = request.json or {}
    target_uid   = data.get("uid")
    target_state = data.get("is_active", False)
    if not target_uid:
        return jsonify({"error": "Missing uid"}), 400
    db.collection("users").document(target_uid).update({"is_active": target_state})
    return jsonify({"status": "success", "message": "Suspension toggled."}), 200


# =============================================================================
# POST /api/l0/users/<target_tenant>/mint
# =============================================================================
@bp.route("/api/l0/users/<string:target_tenant>/mint", methods=["POST"])
@require_auth
@require_super_admin
def mint_credits(uid, tenant_id, user_role, target_tenant):
    from google.cloud import firestore
    amount = float(request.json.get("amount", 0)) if request.json else 0
    if amount <= 0:
        return jsonify({"error": "Invalid mint amount"}), 400
    db.collection("users").document(target_tenant).update(
        {"wallet.allocated_credits": firestore.Increment(int(amount))}
    )
    log.info("credits_minted", target=target_tenant, amount=int(amount))
    return jsonify({"status": "success", "message": f"Minted {int(amount)} credits."}), 200


# =============================================================================
# POST /api/l0/users/<target_tenant>/approve
# =============================================================================
@bp.route("/api/l0/users/<string:target_tenant>/approve", methods=["POST"])
@require_auth
@require_super_admin
def approve_user(uid, tenant_id, user_role, target_tenant):
    from google.cloud import firestore
    payload = request.json or {}
    amount  = int(payload.get("amount", 20000))
    days    = int(payload.get("days", 180))
    expiry  = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
    db.collection("users").document(target_tenant).update({
        "approval_status":         "approved",
        "beta_expiry":             expiry,
        "wallet.allocated_credits": firestore.Increment(amount),
    })
    log.info("user_approved", target=target_tenant, credits=amount, days=days)
    return jsonify({"status": "success", "message": f"Approved with {amount} credits for {days} days."}), 200


# =============================================================================
# GET /api/l0/system-health
# =============================================================================
@bp.route("/api/l0/system-health", methods=["GET"])
@require_auth
@require_super_admin
def get_system_health(uid, tenant_id, user_role):
    now_h  = datetime.datetime.now(datetime.timezone.utc)
    health: dict = {}

    # Circuit breaker state
    try:
        cb_doc  = db.collection("system_telemetry").document("circuit_breaker_state").get()
        cb_data = cb_doc.to_dict() if cb_doc.exists else {}
        serper_total  = int(cb_data.get("serper_calls_window",  0))
        serper_429s   = int(cb_data.get("serper_429s_window",   0))
        scraper_total = int(cb_data.get("scraper_calls_window", 0))
        scraper_ooms  = int(cb_data.get("scraper_ooms_window",  0))
        serper_rate   = round(serper_429s  / serper_total  if serper_total  > 10 else 0.0, 4)
        scraper_rate  = round(scraper_ooms / scraper_total if scraper_total > 10 else 0.0, 4)
        cb_open       = (serper_rate > 0.15) or (scraper_rate > 0.05)
        last_open_at  = cb_data.get("last_open_at")
        health["circuit_breaker"] = {
            "state": "OPEN" if cb_open else "CLOSED",
            "serper_429_rate": serper_rate, "scraper_oom_rate": scraper_rate,
            "serper_calls": serper_total, "serper_429s": serper_429s,
            "scraper_calls": scraper_total, "scraper_ooms": scraper_ooms,
            "last_open_at": last_open_at.isoformat() if last_open_at and hasattr(last_open_at, "isoformat") else None,
            "last_open_reason": cb_data.get("last_open_reason", ""),
        }
    except Exception as cb_e:
        health["circuit_breaker"] = {"state": "UNKNOWN", "error": str(cb_e)}

    # Lead velocity (24h)
    try:
        cutoff      = now_h - datetime.timedelta(hours=24)
        health["leads_last_24h"] = len(list(
            db.collection("leads").where(field_path="createdAt", op_string=">=", value=cutoff).limit(500).stream()
        ))
    except Exception:
        health["leads_last_24h"] = None

    # Active campaigns
    try:
        health["active_campaigns"] = len(list(
            db.collection("campaigns").where(field_path="status", op_string="==", value="active").limit(500).stream()
        ))
    except Exception:
        health["active_campaigns"] = None

    # Rejected lead count
    try:
        health["total_rejected"] = len(list(
            db.collection("leads").where(field_path="status", op_string="==", value="rejected").limit(500).stream()
        ))
    except Exception:
        health["total_rejected"] = None

    # Ontology map size
    try:
        health["ontology_domains"] = len(list(db.collection("ontology_map").limit(200).stream()))
    except Exception:
        health["ontology_domains"] = None

    health["generated_at"] = now_h.isoformat()
    return jsonify({"status": "success", "data": health}), 200


# =============================================================================
# GET /api/l0/shadow-ledger
# =============================================================================
@bp.route("/api/l0/shadow-ledger", methods=["GET"])
@require_auth
@require_super_admin
def get_shadow_ledger(uid, tenant_id, user_role):
    from google.cloud import firestore
    limit = min(int(request.args.get("limit", 200)), 500)
    try:
        rej_docs = (
            db.collection("leads")
              .where(field_path="status", op_string="==", value="rejected")
              .order_by("updatedAt", direction=firestore.Query.DESCENDING)
              .limit(limit)
              .stream()
        )
        leads_out = []
        for doc in rej_docs:
            d = doc.to_dict() or {}
            leads_out.append({
                "id":                  doc.id,
                "source_url":          d.get("source_url", d.get("url", "")),
                "base_path":           d.get("base_path", ""),
                "company_domain":      d.get("company_domain", ""),
                "domain":              d.get("domain", ""),
                "score":               d.get("score"),
                "tenant_id":           d.get("tenant_id", ""),
                "rejection_reason":    d.get("rejection_reason"),
                "ai_rejection_reason": d.get("ai_rejection_reason", d.get("rejection_signal", "")),
                "status":              d.get("status"),
                "updatedAt":           d["updatedAt"].isoformat() if d.get("updatedAt") and hasattr(d["updatedAt"], "isoformat") else None,
            })
        return jsonify({"status": "success", "leads": leads_out, "count": len(leads_out)}), 200
    except Exception as e:
        return jsonify({"error": "Shadow Ledger query failed", "message": str(e)}), 500


# =============================================================================
# GET /api/internal/l0/operations-telemetry  (TTL-cached, 5 min)
# =============================================================================
@bp.route("/api/internal/l0/operations-telemetry", methods=["GET"])
@require_auth
@require_super_admin
def get_ops_telemetry(uid, tenant_id, user_role):
    from google.cloud import bigquery, firestore

    CACHE_KEY = "ops_telemetry_v1"

    if CACHE_KEY in _ops_cache:
        cached = _ops_cache[CACHE_KEY]
        return jsonify({"status": "success", "cache_hit": True,
                        "cached_at": cached["cached_at"].isoformat(), "data": cached["data"]}), 200

    # 1. Firestore geo primary
    geo_heatmap: list = []
    try:
        fs_geo: dict = {}
        for cdoc in db.collection("campaigns").limit(500).stream():
            cd = cdoc.to_dict() or {}
            if cd.get("status", "active") != "active":
                continue
            region = ((cd.get("gl") or cd.get("location") or "Unknown") or "Unknown").strip() or "Unknown"
            fs_geo[region] = fs_geo.get(region, 0) + 1
        geo_heatmap = [{"region": k, "active_campaigns": v} for k, v in sorted(fs_geo.items(), key=lambda x: -x[1])]
    except Exception as fs_e:
        log.warning("ops_telemetry_firestore_geo_failed", error=str(fs_e))

    # 2. BQ enrichment (optional)
    bq_used = False
    try:
        bq      = bigquery.Client(project=PROJECT_ID)
        bq_sql  = f"""
            SELECT COALESCE(NULLIF(TRIM(JSON_EXTRACT_SCALAR(data,'$.gl')),''),
                            NULLIF(TRIM(JSON_EXTRACT_SCALAR(data,'$.location')),''), 'Unknown') AS region,
                   COUNT(*) AS active_campaigns
            FROM `{PROJECT_ID}.firestore_export.campaigns_raw`
            WHERE JSON_EXTRACT_SCALAR(data,'$.status')='active'
            GROUP BY region ORDER BY active_campaigns DESC LIMIT 50
        """
        bq_rows    = bq.query(bq_sql).result()
        bq_heatmap = [{"region": (r.region or "Unknown").strip(), "active_campaigns": int(r.active_campaigns)} for r in bq_rows]
        if bq_heatmap:
            geo_heatmap = bq_heatmap
            bq_used = True
    except Exception:
        pass  # Silent fallback to Firestore data

    # 3. Domain affinity matrix
    domain_matrix: list = []
    fs_error = None
    try:
        from google.cloud import firestore as _fs
        ont_docs = (
            db.collection("ontology_map")
              .order_by("baseline_weight", direction=_fs.Query.DESCENDING)
              .limit(10).stream()
        )
        for doc in ont_docs:
            d = doc.to_dict() or {}
            domain_matrix.append({
                "domain":          d.get("base_path", doc.id),
                "baseline_weight": round(float(d.get("baseline_weight", 1.0)), 4),
                "total_yield":     int(d.get("total_yield", 0)),
                "last_seen":       d["last_seen"].isoformat() if d.get("last_seen") and hasattr(d["last_seen"], "isoformat") else None,
            })
    except Exception as fs_e:
        fs_error = str(fs_e)

    now     = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "geo_heatmap":   geo_heatmap,
        "domain_matrix": domain_matrix,
        "generated_at":  now.isoformat(),
        "ttl_seconds":   _OPS_CACHE_TTL,
        "geo_source":    "bigquery" if bq_used else "firestore",
    }
    if fs_error:
        payload["partial_errors"] = {"firestore": fs_error}

    if geo_heatmap and not fs_error:
        _ops_cache[CACHE_KEY] = {"data": payload, "cached_at": now}

    return jsonify({"status": "success", "cache_hit": False, "data": payload}), 200
