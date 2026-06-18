"""
Orchestrator V23 — /api/leads/<id> Blueprint.

Routes:
  PUT /api/leads/<id>  — RLHF backpropagation + Shadow Tracker + Negative Signal
                          + Ontology weight update + CRM egress webhook
"""
from __future__ import annotations

import httpx

from flask import Blueprint, jsonify, request
from google.cloud.firestore_v1.base_query import FieldFilter

from core.clients import get_db  # type: ignore[import]
from core.auth import require_auth  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]
from core.helpers import (  # type: ignore[import]
    parse_base_path,
    _async_neg_signal_insert,
    _async_shadow_track,
    _enqueue_bq_telemetry_task,
)

def _db():
    return get_db()

bp = Blueprint("leads", __name__)
log = get_logger("orchestrator.v23.leads")

NEG_SIGNAL_REASONS = frozenset({"competitor", "author"})

REJECTION_PENALTY_MAP: dict[str, float] = {
    "not_b2b":        -0.25,
    "bad_data":       -0.20,
    "wrong_industry": -0.15,
    "too_small":      -0.05,
    "competitor":      0.00,
    "author":          0.00,
}


# =============================================================================
# PUT /api/leads/<id>
# =============================================================================
@bp.route("/api/leads/<string:doc_id>", methods=["PUT"])
@require_auth
def update_lead(uid, tenant_id, user_role, doc_id):
    """
    Update lead status. Triggers:
    - RLHF backpropagation (converted / ignored)
    - Shadow Tracker N-gram upsert (converted)
    - Categorical Rejection Engine + Ontology penalty (rejected)
    - Negative Signal BQ insert (competitor / author)
    - BQ RLHF telemetry enqueue
    - Headless CRM egress webhook
    """
    from google.cloud import firestore  # SERVER_TIMESTAMP

    doc_ref  = _db().collection("leads").document(doc_id)
    doc_data = doc_ref.get()

    if not doc_data.exists or doc_data.to_dict().get("tenant_id") != tenant_id:
        return jsonify({"error": "Forbidden"}), 403

    data = request.json or {}
    data.pop("tenant_id", None)
    data["updatedAt"] = firestore.SERVER_TIMESTAMP

    # Persist the update
    if "interactions" in data:
        db_interaction = {"action": data.get("interactions", ""), "date": firestore.SERVER_TIMESTAMP}
        doc_ref.update({
            "status":       data.get("status"),
            "updatedAt":    firestore.SERVER_TIMESTAMP,
            "interactions": firestore.ArrayUnion([db_interaction]),
        })
    else:
        doc_ref.update(data)

    status           = data.get("status")
    lead_dict        = doc_data.to_dict()
    tech_stack       = lead_dict.get("tech_stack_found", [])
    hiring_intent    = lead_dict.get("hiring_intent_found", "")
    rejection_reason = data.get("rejection_reason")

    # ── RLHF Backpropagation ──────────────────────────────────────────────────
    if status in ("converted", "ignored"):
        import re
        delta       = 1 if status == "converted" else -1
        user_ref    = _db().collection("users").document(tenant_id)
        pref_updates: dict = {}

        if hiring_intent and hiring_intent.lower() != "none":
            pref_updates["preferences_weights.hiring_intent"] = firestore.Increment(delta)
        for tech in tech_stack:
            pref_updates[f"preferences_weights.tech_{tech}"] = firestore.Increment(delta)
        if status == "ignored":
            pain_point = lead_dict.get("pain_point", "")
            words      = list(set(re.findall(r"\b\w{4,}\b", pain_point.lower())))
            extracted  = words[:3]
            if isinstance(tech_stack, list) and tech_stack:
                extracted.extend([t.lower() for t in tech_stack[:2]])
            if extracted:
                pref_updates["dynamic_blocklist"] = firestore.ArrayUnion(extracted)
        if pref_updates:
            try:
                user_ref.set(pref_updates, merge=True)
            except Exception as e:
                log.warning("rlhf_backprop_failed", error=str(e))

    # ── Shadow Tracker (converted only) ──────────────────────────────────────
    if status == "converted":
        try:
            pain_st     = (lead_dict.get("pain_point") or "").strip()
            camp_id_st  = lead_dict.get("campaign_id") or ""
            persona_cat = "general"
            if camp_id_st:
                camp_snap  = db.collection("campaigns").document(camp_id_st).get()
                camp_dict  = camp_snap.to_dict() if camp_snap.exists else {}
                persona_cat = (camp_dict.get("persona_name") or camp_dict.get("name") or "general").strip()
            if pain_st and persona_cat:
                _async_shadow_track(persona_category=persona_cat, pain_point=pain_st, tenant_id=tenant_id)
        except Exception as st_e:
            log.warning("shadow_tracker_hook_failed", error=str(st_e))

    # ── CRM Ontology RLHF (won / negotiating / lost) ─────────────────────────
    crm_status = data.get("crm_status")
    if crm_status in ("won", "negotiating", "lost"):
        source_url    = lead_dict.get("source_url", lead_dict.get("url", ""))
        base_path_key = parse_base_path(source_url)
        if base_path_key and base_path_key != "unknown":
            try:
                ont_ref  = db.collection("ontology_map").document(base_path_key)
                ont_snap = ont_ref.get()
                if ont_snap.exists:
                    total_yield = ont_snap.to_dict().get("total_yield", 0)
                    if total_yield >= 50:
                        delta_w = 0.15 if crm_status in ("won", "negotiating") else -0.05
                        ont_ref.update({"baseline_weight": firestore.Increment(delta_w), "last_seen": firestore.SERVER_TIMESTAMP})
                        log.info("ontology_rlhf_applied", base_path=base_path_key, delta=delta_w)
            except Exception as re_err:
                log.warning("ontology_rlhf_failed", error=str(re_err))

    # ── Categorical Rejection Engine (rejected with reason) ──────────────────
    if status == "rejected" and rejection_reason:
        if rejection_reason not in REJECTION_PENALTY_MAP:
            log.warning("invalid_rejection_reason", reason=rejection_reason)
        else:
            penalty = REJECTION_PENALTY_MAP[rejection_reason]
            try:
                doc_ref.update({
                    "rejection_reason": rejection_reason,
                    "status":           "rejected",
                    "updatedAt":        firestore.SERVER_TIMESTAMP,
                })
            except Exception as rej_e:
                log.warning("rejection_lead_update_failed", error=str(rej_e))

            if penalty != 0.0:
                try:
                    src_url = lead_dict.get("source_url", lead_dict.get("url", ""))
                    bpk     = parse_base_path(src_url)
                    if bpk and bpk != "unknown":
                        ont_ref  = db.collection("ontology_map").document(bpk)
                        ont_snap = ont_ref.get()
                        if ont_snap.exists:
                            ont_ref.update({
                                "baseline_weight": firestore.Increment(penalty),
                                "last_seen":       firestore.SERVER_TIMESTAMP,
                                f"rejection_counts.{rejection_reason}": firestore.Increment(1),
                            })
                except Exception as rlhf_e:
                    log.warning("rejection_ontology_failed", error=str(rlhf_e))

    # ── Negative Signal BQ insert ─────────────────────────────────────────────
    if status == "rejected" and rejection_reason in NEG_SIGNAL_REASONS:
        try:
            entity_name = (
                lead_dict.get("company_name") or lead_dict.get("dm") or lead_dict.get("name") or ""
            ).strip()
            raw_url     = lead_dict.get("source_url") or lead_dict.get("url") or ""
            root_domain = parse_base_path(raw_url).split("/")[0]  # domain-only
            if root_domain or entity_name:
                _async_neg_signal_insert(
                    entity_name=entity_name, root_domain=root_domain,
                    rejection_reason=rejection_reason, tenant_id=tenant_id,
                )
        except Exception as ns_e:
            log.warning("neg_signal_hook_failed", error=str(ns_e))

    # ── BQ RLHF telemetry enqueue ─────────────────────────────────────────────
    bq_status = data.get("status") or data.get("crm_status") or "updated"
    _enqueue_bq_telemetry_task(tenant_id, lead_dict, bq_status)

    # ── Headless CRM egress webhook (converted) ───────────────────────────────
    if status == "converted":
        try:
            user_crm_doc    = db.collection("users").document(tenant_id).get().to_dict() or {}
            crm_webhook_url = user_crm_doc.get("crm_webhook_url")
            if crm_webhook_url:
                crm_payload = {
                    "lead_id":           doc_id,
                    "score":             lead_dict.get("score"),
                    "dm":                lead_dict.get("dm"),
                    "intent_signal":     lead_dict.get("intent_signal", ""),
                    "contact_endpoints": lead_dict.get("contact_endpoints", []),
                }
                httpx.post(crm_webhook_url, json=crm_payload, timeout=5)
        except Exception as crm_e:
            log.warning("crm_egress_failed", error=str(crm_e))

    return jsonify({"status": "success"}), 200


# =============================================================================
# GET /api/inbound-signals
# V23.5 — Return inbound radar signals for the authenticated tenant
# =============================================================================
@bp.route("/api/inbound-signals", methods=["GET"])
@require_auth
def list_inbound_signals(uid, tenant_id, user_role):
    """
    List inbound sentiment signals for the current tenant.

    Query params:
      status        (str)   — filter by status: new | reviewed | converted_to_lead | dismissed
                              default: new
      intent_label  (str)   — filter by ACTIVE_SEEKING | EXPRESSING_PAIN | COMPETITOR_CHURN | TREND
      campaign_id   (str)   — filter by campaign ID (optional)
      limit         (int)   — max results 1–50, default 20
    """
    import traceback
    try:
        # Check tenant_id context resolution defensively
        t_id = tenant_id or uid
        if not t_id:
            log.error("list_inbound_signals_missing_tenant_and_uid")
            return jsonify({"signals": [], "error": True}), 400

        status       = request.args.get("status", "new")
        intent_label = request.args.get("intent_label")
        campaign_id  = request.args.get("campaign_id") or request.args.get("campaign")

        # Verify that request.args.get('limit') or similar parameters are safely cast to integers
        raw_limit = request.args.get("limit")
        try:
            if raw_limit is None or str(raw_limit).strip() == "":
                limit = 20
            else:
                limit = int(raw_limit)
        except (ValueError, TypeError):
            limit = 20

        limit = min(max(1, limit), 50)

        valid_statuses = {"new", "reviewed", "converted_to_lead", "dismissed"}
        if status not in valid_statuses:
            return jsonify({"error": f"status must be one of {sorted(valid_statuses)}"}), 400

        from google.cloud import firestore as fs

        def _sanitize_signal_doc(doc) -> dict:
            data = doc.to_dict() or {}
            data["id"] = doc.id
            for k, v in data.items():
                if hasattr(v, "isoformat"):
                    data[k] = v.isoformat()
            return data

        signals = []
        try:
            query = (
                _db().collection("inbound_signals")
                .where(filter=fs.FieldFilter("tenant_id", "==", t_id))
            )
            signals = [_sanitize_signal_doc(d) for d in query.stream()]
        except Exception as exc:
            log.warning("list_inbound_signals_query_failed_graceful_fallback", error=str(exc))
            signals = []

        # Execute Application-Level Filtering: Once the payload documents stream into the Python runtime stack,
        # filter out records where status != "new" or intent_score < 0.55 in-memory.
        signals = [
            s for s in signals
            if s.get("status") == status
            and float(s.get("intent_score", 0.0)) >= 0.55
        ]

        # Optional label filter — applied after Firestore fetch (no composite index needed)
        if intent_label:
            signals = [s for s in signals if s.get("intent_label") == intent_label]

        # Optional campaign filter — applied after Firestore fetch (no composite index needed)
        if campaign_id:
            signals = [s for s in signals if s.get("matched_campaign_id") == campaign_id]

        # Sort and Slice: Sort the remaining validated objects descending by their intent_score attribute,
        # and slice the array to match the defensive request limit size.
        signals.sort(key=lambda s: float(s.get("intent_score", 0.0)), reverse=True)
        signals = signals[:limit]

        return jsonify({"signals": signals, "count": len(signals)}), 200

    except Exception as e:
        traceback.print_exc()
        log.error("list_inbound_signals_fatal_ingress_error", error=str(e), traceback=traceback.format_exc())
        return jsonify({"signals": [], "error": True}), 200


# =============================================================================
# PUT /api/inbound-signals/<signal_doc_id>/status
# V23.5 — Update signal status; optionally promote to a full lead
# =============================================================================
@bp.route("/api/inbound-signals/<string:signal_doc_id>/status", methods=["PUT"])
@require_auth
def update_signal_status(uid, tenant_id, user_role, signal_doc_id):
    """
    Transition an inbound signal's status.

    Body:
      { "status": "reviewed" | "dismissed" | "converted_to_lead" }

    If status == "converted_to_lead":
      - Creates a new document in the leads collection pre-populated from signal data
      - Returns the new lead_id in the response
    """
    from google.cloud import firestore as fs

    body       = request.get_json(silent=True) or {}
    new_status = body.get("status", "").strip()

    valid_transitions = {"reviewed", "dismissed", "converted_to_lead"}
    if new_status not in valid_transitions:
        return jsonify({"error": f"status must be one of {sorted(valid_transitions)}"}), 400

    # Verify ownership — signal doc_id is {uid}_{signal_id} but we accept full doc_id
    signal_ref  = _db().collection("inbound_signals").document(signal_doc_id)
    signal_snap = signal_ref.get()

    if not signal_snap.exists:
        return jsonify({"error": "Signal not found"}), 404

    sig = signal_snap.to_dict() or {}
    if sig.get("tenant_id") != uid:
        return jsonify({"error": "Forbidden"}), 403

    # Update signal status
    signal_ref.update({
        "status":     new_status,
        "updated_at": fs.SERVER_TIMESTAMP,
    })

    lead_id = None

    if new_status == "converted_to_lead":
        # Promote signal to a full lead document
        lead_ref = _db().collection("leads").document()
        company  = sig.get("company_name") or "Unknown Company"
        lead_ref.set({
            "uid":                uid,
            "tenant_id":          uid,
            "url":                sig.get("source_url", ""),
            "company":            company,
            "company_name":       company,
            "summary":            sig.get("snippet", ""),
            "pain_point":         ", ".join(sig.get("pain_keywords", [])),
            "score":              round(sig.get("intent_score", 0.5) * 100),
            "source":             "inbound_radar",
            "status":             "new",
            "fit_score":          sig.get("intent_score", 0.5),
            "intent_label":       sig.get("intent_label", "EXPRESSING_PAIN"),
            "inbound_signal_id":  sig.get("signal_id", ""),
            "inbound_platform":   sig.get("source_platform", "web"),
            "matched_campaigns":  (
                [sig["matched_campaign_id"]]
                if sig.get("matched_campaign_id") else []
            ),
            "createdAt":          fs.SERVER_TIMESTAMP,
            "updatedAt":          fs.SERVER_TIMESTAMP,
        })
        lead_id = lead_ref.id
        log.info(
            "inbound_signal_converted_to_lead",
            uid=uid[:8],
            signal_id=sig.get("signal_id", ""),
            lead_id=lead_id,
        )

    resp = {"status": new_status}
    if lead_id:
        resp["lead_id"] = lead_id
    return jsonify(resp), 200
