"""
Orchestrator — Visitor Signal Ingestion Router (V24.0)

Receives anonymous visitor beacons from sideio-tracker.js embedded on
customer websites. Writes visitor events to Firestore for later
company resolution and Signal Graph integration.

Endpoints:
    POST /api/visitor-signals  — ingest a visitor beacon

Privacy:
    - No cookies set
    - No PII stored (IP used only for company resolution, not persisted)
    - Rate-limited per tenant (100 req/s)
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from google.cloud import firestore as fs

visitor_bp = Blueprint("visitor_signals", __name__)

# Simple in-memory rate limiter: {tenant_id: (count, window_start)}
_rate_cache: dict[str, tuple[int, float]] = {}
_RATE_LIMIT = 100  # requests per window
_RATE_WINDOW = 60  # seconds


def _check_rate_limit(tenant_id: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    now = time.time()
    entry = _rate_cache.get(tenant_id)
    if not entry or (now - entry[1]) > _RATE_WINDOW:
        _rate_cache[tenant_id] = (1, now)
        return True
    count, window_start = entry
    if count >= _RATE_LIMIT:
        return False
    _rate_cache[tenant_id] = (count + 1, window_start)
    return True


@visitor_bp.route("/api/visitor-signals", methods=["POST", "OPTIONS"])
def ingest_visitor_signal():
    """Ingest a visitor beacon from sideio-tracker.js.
    
    No auth required (public endpoint for customer website embeds).
    Rate-limited per tenant_id.
    """
    # CORS preflight
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp, 204

    body = request.get_json(force=True, silent=True) or {}
    tenant_id = (body.get("tenant_id") or "").strip()
    
    if not tenant_id or len(tenant_id) > 128:
        return jsonify({"error": "Invalid tenant_id"}), 400
    
    if not _check_rate_limit(tenant_id):
        return jsonify({"error": "Rate limited"}), 429

    # V24.5 (L7-1): Visitor signal IP→company enrichment provides zero value for
    # B2C/D2C/B2B2C tenants — individual consumers resolve to their ISP, not a
    # company. Check the tenant's active campaign types. If visitor_signals_enabled
    # is explicitly set to False, skip the write and return 204.
    try:
        _user_doc = fs.Client().collection("users").document(tenant_id).get()
        _user_data = _user_doc.to_dict() or {} if _user_doc.exists else {}
        if _user_data.get("visitor_signals_enabled") is False:
            return "", 204  # Opt-out
    except Exception:
        pass  # Fail-open: write the signal if flag check fails

    page_url = (body.get("page_url") or "")[:2048]
    referrer = (body.get("referrer") or "")[:2048]
    page_title = (body.get("page_title") or "")[:512]
    
    # Generate a dedup key from page_url + IP (IP not stored)
    remote_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    remote_ip = remote_ip.split(",")[0].strip() if remote_ip else ""
    visit_hash = hashlib.sha256(f"{page_url}:{remote_ip}:{tenant_id}".encode()).hexdigest()[:16]
    
    doc_data = {
        "tenant_id": tenant_id,
        "page_url": page_url,
        "referrer": referrer,
        "page_title": page_title,
        "screen_width": body.get("screen_width"),
        "visit_hash": visit_hash,
        "ip_hash": hashlib.sha256(remote_ip.encode()).hexdigest()[:16] if remote_ip else None,
        "created_at": datetime.now(timezone.utc),
        # Reverse DNS will be resolved async by signal-stacking cron
        "company_resolved": False,
        "company_name": None,
    }
    
    try:
        db = fs.Client()
        doc_ref = db.collection("visitor_signals").document(f"{tenant_id}_{visit_hash}")
        doc_ref.set(doc_data, merge=True)
    except Exception as exc:
        # Log but don't fail the beacon — fire-and-forget semantics
        import logging
        logging.getLogger("orchestrator.visitor_signals").warning(
            "visitor_signal_write_failed: tenant=%s err=%s", tenant_id, exc
        )
    
    resp = jsonify({"ok": True})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 202
