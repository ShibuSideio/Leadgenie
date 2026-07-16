"""
Orchestrator — Visitor Signal Ingestion Router (V24.0)

Receives anonymous visitor beacons from sideio-tracker.js embedded on
customer websites. Writes visitor events to Firestore for later
company resolution and Signal Graph integration.

Domain intelligence (V26.4+):
    When the tenant has an active campaign with ``system_domain_profile``,
    beacons are stamped with domain metadata (family, confidence tier,
    strictness_bias) and an ``enrichment_priority`` so downstream company
    resolution can treat thin/low-confidence campaigns more gently.
    No profile → identical legacy payload (backward compatible).

Endpoints:
    POST /api/visitor-signals  — ingest a visitor beacon

Privacy:
    - No cookies set
    - No PII stored (IP used only for company resolution, not persisted)
    - Rate-limited per tenant (100 req/s)
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from flask import Blueprint, request, jsonify
from google.cloud import firestore as fs

from core.logging import get_logger  # type: ignore[import]
from shared.domain_gate import (  # type: ignore[import]
    compute_enrichment_priority,
    compute_intent_threshold,
    enrichment_plan_for_priority,
    extract_domain_meta,
    profile_confidence_label,
)

visitor_bp = Blueprint("visitor_signals", __name__)
log = get_logger("orchestrator.visitor_signals")

# Simple in-memory rate limiter: {tenant_id: (count, window_start)}
_rate_cache: dict[str, tuple[int, float]] = {}
_RATE_LIMIT = 100  # requests per window
_RATE_WINDOW = 60  # seconds

# SEC-05: Tenant existence cache — {tenant_id: expiry_timestamp}
# Avoids Firestore reads on every beacon while ensuring only valid tenants write data.
_TENANT_CACHE: dict[str, float] = {}
_TENANT_CACHE_TTL = 300  # 5 minutes

# Cache resolved domain profile per tenant to keep beacon path cheap.
# {tenant_id: (profile_or_None, campaign_id_or_None, expiry_ts)}
_DOMAIN_PROFILE_CACHE: dict[str, tuple[Optional[dict], Optional[str], float]] = {}
_DOMAIN_PROFILE_CACHE_TTL = 300  # 5 minutes


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


def _db() -> fs.Client:
    return fs.Client()


def _select_domain_profile_from_campaigns(
    campaigns: list[dict[str, Any]],
) -> tuple[Optional[dict], Optional[str]]:
    """Pick the best system_domain_profile among active campaigns.

    Preference:
      1. Manual override_active profiles
      2. Higher profile_confidence (high > medium > low)
      3. Higher numeric confidence
    Returns (profile, campaign_id) or (None, None).
    """
    if not campaigns:
        return None, None

    conf_rank = {"high": 3, "medium": 2, "low": 1}
    best: Optional[dict] = None
    best_cid: Optional[str] = None
    best_key: tuple = (-1, -1, -1.0)

    for camp in campaigns:
        if not isinstance(camp, dict):
            continue
        profile = camp.get("system_domain_profile")
        if not isinstance(profile, dict):
            continue
        if not (profile.get("domain_family") or profile.get("strictness_bias") is not None):
            continue
        override = 1 if profile.get("override_active") else 0
        tier = conf_rank.get(profile_confidence_label(profile), 0)
        try:
            numeric = float(profile.get("confidence") or 0.0)
        except (TypeError, ValueError):
            numeric = 0.0
        key = (override, tier, numeric)
        if key > best_key:
            best_key = key
            best = profile
            best_cid = str(camp.get("campaign_id") or camp.get("id") or "") or None

    return best, best_cid


def _load_tenant_domain_profile(
    db: fs.Client,
    tenant_id: str,
    preferred_campaign_id: str = "",
) -> tuple[Optional[dict], Optional[str]]:
    """Load domain profile for tenant (cached). Prefer optional campaign_id."""
    now = time.time()
    cache_key = f"{tenant_id}|{preferred_campaign_id or '*'}"
    cached = _DOMAIN_PROFILE_CACHE.get(cache_key)
    if cached and now < cached[2]:
        return cached[0], cached[1]

    profile: Optional[dict] = None
    campaign_id: Optional[str] = None

    try:
        if preferred_campaign_id:
            snap = db.collection("campaigns").document(preferred_campaign_id).get()
            if snap.exists:
                data = snap.to_dict() or {}
                if data.get("tenant_id") == tenant_id or data.get("uid") == tenant_id:
                    p = data.get("system_domain_profile")
                    if isinstance(p, dict) and (
                        p.get("domain_family") or p.get("strictness_bias") is not None
                    ):
                        profile, campaign_id = p, preferred_campaign_id

        if profile is None:
            camps: list[dict[str, Any]] = []
            for field in ("tenant_id", "uid"):
                try:
                    docs = (
                        db.collection("campaigns")
                        .where(filter=fs.FieldFilter(field, "==", tenant_id))
                        .where(filter=fs.FieldFilter("status", "==", "active"))
                        .limit(5)
                        .stream()
                    )
                    for d in docs:
                        row = d.to_dict() or {}
                        row["campaign_id"] = d.id
                        camps.append(row)
                    if camps:
                        break
                except Exception:
                    continue
            profile, campaign_id = _select_domain_profile_from_campaigns(camps)
    except Exception as exc:
        log.warning(
            "visitor_domain_profile_load_failed",
            tenant_id=tenant_id[:12],
            error=str(exc),
            note="Continuing without domain profile (legacy beacon path).",
        )
        profile, campaign_id = None, None

    _DOMAIN_PROFILE_CACHE[cache_key] = (profile, campaign_id, now + _DOMAIN_PROFILE_CACHE_TTL)
    return profile, campaign_id


def _enrichment_priority_for_profile(
    domain_profile: Mapping[str, Any] | None,
    *,
    sourcing_vector: str | None = None,
) -> str:
    """Backward-compatible wrapper around shared.compute_enrichment_priority."""
    priority, _ = compute_enrichment_priority(
        domain_profile,
        sourcing_vector=sourcing_vector,
    )
    return priority


def _build_domain_fields(
    domain_profile: Mapping[str, Any] | None,
    campaign_id: Optional[str],
    *,
    sourcing_vector: str | None = None,
) -> dict[str, Any]:
    """Domain metadata + actionable enrichment priority for visitor_signals.

    Returns empty dict when no profile (exact legacy write shape).
    """
    if not isinstance(domain_profile, Mapping) or not domain_profile:
        return {}

    meta = extract_domain_meta(domain_profile)
    if not meta.get("domain_family") and meta.get("strictness_bias") is None:
        return {}

    # Reuse shared threshold helper for observability parity with inbound radar.
    # Base 0.45 is the sentiment write floor; we only log the delta for visitors.
    _, thresh_meta = compute_intent_threshold(
        0.45,
        domain_profile,
        floor=0.35,
        ceiling=0.60,
        bias_unit=0.12,
    )

    priority, prio_meta = compute_enrichment_priority(
        domain_profile,
        sourcing_vector=sourcing_vector,
    )
    plan = enrichment_plan_for_priority(priority)

    fields: dict[str, Any] = {
        "domain_family": meta.get("domain_family"),
        "domain_source": meta.get("domain_source") or "system_domain_profile",
        "profile_confidence": meta.get("profile_confidence"),
        "thin_campaign": bool(meta.get("thin_campaign")),
        "strictness_bias": meta.get("strictness_bias"),
        # Actionable prioritization for reverse-IP / firmographic workers:
        # sort by enrichment_priority_rank asc, then apply enrichment_plan.
        "enrichment_priority": priority,
        "enrichment_priority_rank": plan.get("rank"),
        "enrichment_queue": plan.get("queue"),
        "enrichment_resolve_company": plan.get("resolve_company"),
        "enrichment_max_lookups": plan.get("max_lookups"),
        "enrichment_score": prio_meta.get("score"),
        "enrichment_reasons": prio_meta.get("reasons"),
        "firmographic_value": prio_meta.get("firmographic_value"),
        "domain_threshold_delta": thresh_meta.get("threshold_delta"),
        "domain_confidence_scale": thresh_meta.get("confidence_scale"),
    }
    if campaign_id:
        fields["matched_campaign_id"] = campaign_id
    # Drop Nones so merge doesn't wipe unrelated fields with null noise
    return {k: v for k, v in fields.items() if v is not None}


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

    # SEC-05: Verify tenant exists in users collection (cached 5 min)
    now_ts = time.time()
    cached_expiry = _TENANT_CACHE.get(tenant_id)
    db = None
    if cached_expiry is not None and now_ts < cached_expiry:
        tenant_valid = True
    else:
        try:
            db = _db()
            _tenant_doc = db.collection("users").document(tenant_id).get()
            tenant_valid = _tenant_doc.exists
        except Exception:
            tenant_valid = False
        if tenant_valid:
            _TENANT_CACHE[tenant_id] = now_ts + _TENANT_CACHE_TTL
        else:
            # Negative cache for 60s to avoid repeated lookups for bad IDs
            _TENANT_CACHE[tenant_id] = now_ts + 60
    if not tenant_valid:
        return jsonify({"error": "Unknown tenant"}), 404

    if not _check_rate_limit(tenant_id):
        return jsonify({"error": "Rate limited"}), 429

    # V24.5 (L7-1): Visitor signal IP→company enrichment provides zero value for
    # B2C/D2C/B2B2C tenants — individual consumers resolve to their ISP, not a
    # company. Check the tenant's active campaign types. If visitor_signals_enabled
    # is explicitly set to False, skip the write and return 204.
    try:
        if db is None:
            db = _db()
        _user_doc = db.collection("users").document(tenant_id).get()
        _user_data = _user_doc.to_dict() or {} if _user_doc.exists else {}
        if _user_data.get("visitor_signals_enabled") is False:
            return "", 204  # Opt-out
    except Exception:
        pass  # Fail-open: write the signal if flag check fails

    page_url = (body.get("page_url") or "")[:2048]
    referrer = (body.get("referrer") or "")[:2048]
    page_title = (body.get("page_title") or "")[:512]
    # Optional: tracker may send campaign_id in future embeds.
    preferred_campaign_id = (body.get("campaign_id") or "").strip()[:128]
    
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

    # ── Domain intelligence (optional) ────────────────────────────────────
    # Visitor beacons are not intent-scored. We stamp domain metadata and
    # enrichment_priority so firmographic resolution can respect thin campaigns.
    try:
        if db is None:
            db = _db()
        domain_profile, matched_campaign_id = _load_tenant_domain_profile(
            db, tenant_id, preferred_campaign_id=preferred_campaign_id
        )
        # Prefer sourcing_vector from matched campaign when available (cached profile
        # path may only have profile dict — vector is optional soft signal).
        _vector = None
        if matched_campaign_id:
            try:
                _c = db.collection("campaigns").document(matched_campaign_id).get()
                if _c.exists:
                    _vector = (_c.to_dict() or {}).get("sourcing_vector")
            except Exception:
                _vector = None
        domain_fields = _build_domain_fields(
            domain_profile,
            matched_campaign_id,
            sourcing_vector=str(_vector) if _vector else None,
        )
        if domain_fields:
            doc_data.update(domain_fields)
            log.info(
                "visitor_domain_profile_used",
                tenant_id=tenant_id[:12],
                domain_family=domain_fields.get("domain_family"),
                profile_confidence=domain_fields.get("profile_confidence"),
                thin_campaign=domain_fields.get("thin_campaign"),
                strictness_bias=domain_fields.get("strictness_bias"),
                domain_source=domain_fields.get("domain_source"),
                matched_campaign_id=domain_fields.get("matched_campaign_id"),
                enrichment_priority=domain_fields.get("enrichment_priority"),
                enrichment_queue=domain_fields.get("enrichment_queue"),
                firmographic_value=domain_fields.get("firmographic_value"),
                enrichment_score=domain_fields.get("enrichment_score"),
            )
            log.info(
                "visitor_enrichment_priority_assigned",
                tenant_id=tenant_id[:12],
                enrichment_priority=domain_fields.get("enrichment_priority"),
                enrichment_priority_rank=domain_fields.get("enrichment_priority_rank"),
                enrichment_queue=domain_fields.get("enrichment_queue"),
                enrichment_resolve_company=domain_fields.get("enrichment_resolve_company"),
                enrichment_max_lookups=domain_fields.get("enrichment_max_lookups"),
                enrichment_reasons=domain_fields.get("enrichment_reasons"),
                note=(
                    "Downstream firmographic workers should sort by "
                    "enrichment_priority_rank and honor enrichment_queue/max_lookups."
                ),
            )
            if domain_fields.get("domain_threshold_delta") not in (None, 0, 0.0):
                log.info(
                    "visitor_domain_adjustment_applied",
                    tenant_id=tenant_id[:12],
                    domain_family=domain_fields.get("domain_family"),
                    profile_confidence=domain_fields.get("profile_confidence"),
                    strictness_bias=domain_fields.get("strictness_bias"),
                    domain_threshold_delta=domain_fields.get("domain_threshold_delta"),
                    domain_confidence_scale=domain_fields.get("domain_confidence_scale"),
                    enrichment_priority=domain_fields.get("enrichment_priority"),
                    note=(
                        "Visitor path has no intent score; domain bias drives "
                        "enrichment_priority and metadata only (not a hard reject)."
                    ),
                )
    except Exception as _dom_exc:
        log.warning(
            "visitor_domain_integration_failed",
            tenant_id=tenant_id[:12],
            error=str(_dom_exc),
            note="Writing beacon without domain fields (legacy-compatible).",
        )
    
    try:
        if db is None:
            db = _db()
        doc_ref = db.collection("visitor_signals").document(f"{tenant_id}_{visit_hash}")
        doc_ref.set(doc_data, merge=True)
    except Exception as exc:
        # Log but don't fail the beacon — fire-and-forget semantics
        log.warning(
            "visitor_signal_write_failed",
            tenant_id=tenant_id[:12],
            error=str(exc),
        )
    
    resp = jsonify({"ok": True})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 202
