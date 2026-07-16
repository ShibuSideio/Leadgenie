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
from api.routers.settings import _is_internal_url  # SEC-01: SSRF validation

def _db():
    return get_db()

bp = Blueprint("leads", __name__)
log = get_logger("orchestrator.v23.leads")

# V24.5 (L8-4): Sync with neg_signal.NEG_SIGNAL_REASONS — all five reasons now
# trigger both the ontology penalty AND the BQ Negative_Signals insert.
# Phase 4C: Extended with granular rejection reasons.
NEG_SIGNAL_REASONS = frozenset({
    "competitor",
    "author",
    "wrong_industry",
    "not_icp",
    "low_quality",
    "wrong_topic",
    "wrong_geography",
    "news_article",
    "too_old",
    "cant_contact",
    "other",
})

# Phase 4C: Granular rejection reasons accepted from the UI.
VALID_REJECTION_REASONS = frozenset({
    "wrong_topic",
    "wrong_geography",
    "news_article",
    "too_old",
    "cant_contact",
    "competitor",
    "other",
})

_LEAD_UPDATE_ALLOWED = {"status", "is_in_crm", "crm_status", "rejection_reason", "deal_value", "follow_up_date", "notes", "crm_notes", "updatedAt"}

REJECTION_PENALTY_MAP: dict[str, float] = {
    "not_b2b":          -0.25,
    "bad_data":         -0.20,
    "wrong_industry":   -0.15,
    "too_small":        -0.05,
    "competitor":        0.00,
    "author":            0.00,
    "not_icp":          -0.10,  # V24.5 (L8-4)
    "low_quality":      -0.10,  # V24.5 (L8-4)
    # Phase 4C: Granular rejection reasons
    "wrong_topic":      -0.10,
    "wrong_geography":  -0.10,
    "news_article":     -0.05,
    "too_old":          -0.05,
    "cant_contact":      0.00,
    "other":             0.00,
}


def _extract_root_domain(url: str) -> str:
    """Extract the root domain from a URL (e.g. 'https://blog.example.com/p' -> 'example.com')."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        hostname = urlparse(url).hostname or ""
        parts = hostname.lower().split(".")
        # Return last two parts for standard domains, handle edge cases
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return hostname
    except Exception:
        return ""


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
    lead_dict = doc_data.to_dict() or {}

    # ── Manual Requeue Gate (V23.7 — Fault Recovery) ─────────────────────────
    # If the UI sends status='queued' (from a failed lead's re-queue button),
    # we intercept here to: validate credits, reset retry state, and dispatch
    # a fresh Cloud Task. This prevents re-processing without sufficient wallet.
    if data.get("status") == "queued" and lead_dict.get("status") == "failed":

        # ── Max manual requeue limit: prevent infinite credit drain ──
        MAX_MANUAL_REQUEUES = 3
        current_manual_requeues = lead_dict.get("manual_requeue_count", 0)
        if current_manual_requeues >= MAX_MANUAL_REQUEUES:
            log.warning("requeue_max_manual_exhausted",
                        doc_id=doc_id, tenant_id=tenant_id,
                        attempts=current_manual_requeues)
            return jsonify({
                "error": f"This lead has been requeued {current_manual_requeues} times. "
                         "The issue is likely permanent — please remove or skip this lead."
            }), 422

        # Credit gate: verify tenant has available credits
        user_doc  = _db().collection("users").document(tenant_id).get()
        if user_doc.exists:
            wallet       = (user_doc.to_dict() or {}).get("wallet", {})
            # V24.4 (L5-2): Read total_consumed (written by atomic settlement) rather than
            # consumed_credits (legacy field). Using the wrong field allows quota-exhausted
            # tenants to bypass the requeue gate if their credits were settled atomically.
            _consumed = max(
                wallet.get("total_consumed", 0),
                wallet.get("consumed_credits", 0),  # legacy fallback
            )
            if wallet.get("allocated_credits", 0) <= _consumed:
                log.warning("requeue_credit_gate_blocked",
                            doc_id=doc_id, tenant_id=tenant_id,
                            total=wallet.get("allocated_credits", 0), reserved=_consumed)
                return jsonify({"error": "Insufficient credits"}), 402

        # Apply clean requeue mutation — V23.9: use DELETE_FIELD to nuke
        # error fields entirely (not just None), preventing worker re-fail.
        # NOTE: retry_count is NOT reset — it informs the zombie sweep's
        # MAX_RETRIES check. Only processing_attempts resets for the worker.
        doc_ref.update({
            "status":                 "queued",
            "lock_entity":            None,
            "error":                  firestore.DELETE_FIELD,
            "error_details":          firestore.DELETE_FIELD,
            "credit_settled":         False,
            "processing_attempts":    0,
            "processing_started_at":  None,
            "manual_requeue_count":   current_manual_requeues + 1,
            "requeue_source":         data.get("requeue_source", "manual_ui"),
            "updatedAt":              firestore.SERVER_TIMESTAMP,
        })
        # Reserve a credit for the re-processing attempt
        try:
            _db().collection("users").document(tenant_id).update(
                {"wallet.reserved_credits": firestore.Increment(1)}
            )
        except Exception as _cred_err:
            log.warning("requeue_credit_reserve_failed", error=str(_cred_err))

        # Dispatch fresh Cloud Task
        try:
            from core.config import (  # type: ignore[import]
                PROJECT_ID, LOCATION, QUEUE, PIPELINE_URL, ORCHESTRATOR_SA_EMAIL
            )
            import google.cloud.tasks_v2 as _tv2
            import json as _json
            _campaign_id = lead_dict.get("campaign_id", "")
            if PIPELINE_URL and ORCHESTRATOR_SA_EMAIL and _campaign_id:
                _tc = _tv2.CloudTasksClient()
                _qp = _tc.queue_path(PROJECT_ID, LOCATION, QUEUE)
                _task_body = _json.dumps({
                    "tenant_id": tenant_id,
                    "campaign_id": _campaign_id,
                }).encode()
                _tc.create_task(request={
                    "parent": _qp,
                    "task": {
                        "http_request": {
                            "http_method": _tv2.HttpMethod.POST,
                            "url":     f"{PIPELINE_URL}/dispatch",
                            "headers": {"Content-Type": "application/json"},
                            "body":    _task_body,
                            "oidc_token": {
                                "service_account_email": ORCHESTRATOR_SA_EMAIL,
                                "audience": PIPELINE_URL,
                            },
                        },
                        "dispatch_deadline": {"seconds": 300},
                    },
                })
                log.info("requeue_task_dispatched",
                         doc_id=doc_id, campaign_id=_campaign_id)
        except Exception as _task_err:
            log.warning("requeue_task_dispatch_failed",
                        doc_id=doc_id, error=str(_task_err))

        log.info("lead_requeued",
                 doc_id=doc_id, tenant_id=tenant_id,
                 source=data.get("requeue_source", "manual_ui"))
        return jsonify({"status": "requeued"}), 200

    # Persist the update
    if "interactions" in data:
        db_interaction = {"action": data.get("interactions", ""), "date": firestore.SERVER_TIMESTAMP}
        doc_ref.update({
            "status":       data.get("status"),
            "updatedAt":    firestore.SERVER_TIMESTAMP,
            "interactions": firestore.ArrayUnion([db_interaction]),
        })
    else:
        data = {k: v for k, v in data.items() if k in _LEAD_UPDATE_ALLOWED}
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
                camp_snap  = _db().collection("campaigns").document(camp_id_st).get()
                camp_dict  = camp_snap.to_dict() if camp_snap.exists else {}
                persona_cat = (camp_dict.get("persona_name") or camp_dict.get("name") or "general").strip()
                persona_cat = f"{camp_id_st}_{persona_cat}"
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
                ont_ref  = _db().collection("ontology_map").document(base_path_key)
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
                        ont_ref  = _db().collection("ontology_map").document(bpk)
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

    # ── Phase 4C: Rejection Reason Logging ─────────────────────────────────────
    # If status is rejected/ignored and no rejection_reason was provided, default
    # to "unspecified". Log granular rejection reasons for analytics.
    if status in ("rejected", "ignored"):
        _rej_reason = rejection_reason or "unspecified"
        _rej_campaign = lead_dict.get("campaign_id", "")
        log.info("lead_rejection_reason",
                 reason=_rej_reason,
                 campaign_id=_rej_campaign,
                 lead_id=doc_id)

    # ── Negative Signal BQ insert (expanded for 4C) ───────────────────────────
    # Phase 4C: Write rejection_reason alongside existing negative signal data.
    # For rejected leads with a granular reason, always insert the BQ signal
    # regardless of whether the reason is in the legacy NEG_SIGNAL_REASONS set.
    # P1-BIZ-1: Only fire for reasons NOT already handled by the first block
    # to prevent double BQ inserts for overlapping reasons.
    if status in ("rejected", "ignored") and rejection_reason and rejection_reason in VALID_REJECTION_REASONS and rejection_reason not in NEG_SIGNAL_REASONS:
        _4c_reason = rejection_reason
        try:
            entity_name = (
                lead_dict.get("company_name") or lead_dict.get("dm") or lead_dict.get("name") or ""
            ).strip()
            raw_url     = lead_dict.get("source_url") or lead_dict.get("url") or ""
            root_domain = parse_base_path(raw_url).split("/")[0]  # domain-only
            if root_domain or entity_name:
                _async_neg_signal_insert(
                    entity_name=entity_name, root_domain=root_domain,
                    rejection_reason=_4c_reason, tenant_id=tenant_id,
                )
        except Exception as _4c_err:
            log.warning("neg_signal_4c_hook_failed", error=str(_4c_err))

    # ── BQ RLHF telemetry enqueue ─────────────────────────────────────────────
    bq_status = data.get("status") or data.get("crm_status") or "updated"
    _enqueue_bq_telemetry_task(tenant_id, lead_dict, bq_status)

    # ── Phase 4A: Per-Source Accept Rate Tracking ─────────────────────────────
    # Write accepted/rejected counters to the campaign's source_stats subcollection
    # keyed by the lead's source_type.
    if status in ("converted", "ignored", "rejected"):
        _source = lead_dict.get("source_type", "unknown")
        _4a_campaign = lead_dict.get("campaign_id", "")
        if _4a_campaign:
            _action = "approve" if status == "converted" else "reject"
            try:
                _source_stats_ref = _db().collection("campaigns").document(_4a_campaign) \
                                         .collection("source_stats").document(_source)
                if _action == "approve":
                    _source_stats_ref.set({"accepted": firestore.Increment(1)}, merge=True)
                else:
                    _source_stats_ref.set({"rejected": firestore.Increment(1)}, merge=True)
            except Exception:
                pass
            log.info("lead_source_stat_recorded",
                     source=_source, action=_action, campaign_id=_4a_campaign)

    # ── Phase 4B: Accepted Lead Pattern Mining ────────────────────────────────
    # When a lead is accepted (converted), store URL domain, source type, and
    # score in the campaign's accepted_patterns subcollection for downstream
    # pattern mining.
    if status == "converted":
        _4b_campaign = lead_dict.get("campaign_id", "")
        if _4b_campaign:
            try:
                _pattern = {
                    "url_domain":   _extract_root_domain(lead_dict.get("url", "")),
                    "source_type":  lead_dict.get("source_type", "unknown"),
                    "score":        lead_dict.get("score", 0),
                    "accepted_at":  firestore.SERVER_TIMESTAMP,
                }
                _db().collection("campaigns").document(_4b_campaign) \
                     .collection("accepted_patterns").add(_pattern)
            except Exception as _4b_err:
                log.warning("accepted_pattern_mining_failed", error=str(_4b_err))

    # ── Headless CRM egress webhook (converted) ───────────────────────────────
    if status == "converted":
        user_crm_doc    = _db().collection("users").document(tenant_id).get().to_dict() or {}
        crm_webhook_url = user_crm_doc.get("crm_webhook_url")
        if crm_webhook_url:
            # V24.4 (L5-3): CRM webhook delivery with Cloud Task retry.
            # Previous: silent swallow on failure, no retry, HTTP 200 returned.
            # New: on failure, enqueue a retry Cloud Task and record crm_delivery_status.
            crm_delivery_status = "delivered"
            try:
                import httpx as _httpx
                # SEC-01: Validate CRM webhook URL is not an internal/private address
                if _is_internal_url(crm_webhook_url):
                    log.warning("crm_egress_ssrf_blocked",
                                lead_id=doc_id,
                                webhook_url=crm_webhook_url[:60],
                                reason="URL resolves to internal/private address")
                    crm_delivery_status = "blocked_ssrf"
                else:
                    _crm_resp = _httpx.post(
                        crm_webhook_url,
                        json={
                            "lead_id":         doc_id,
                            "score":           lead_dict.get("score"),
                            "dm":              lead_dict.get("dm"),
                            "intent_signal":   lead_dict.get("intent_signal"),
                            "contact_endpoints": lead_dict.get("contact_endpoints", []),
                        },
                        timeout=5,
                    )
                    _crm_resp.raise_for_status()
            except Exception as crm_e:
                log.warning("crm_egress_failed",
                            error=str(crm_e),
                            lead_id=doc_id,
                            webhook_url=crm_webhook_url[:60])
                crm_delivery_status = "pending_retry"
                # Enqueue a single retry Cloud Task (3-hour delay)
                try:
                    from core.config import PROJECT_ID as _pid, LOCATION as _loc, QUEUE as _q, ORCHESTRATOR_URL as _orch  # type: ignore[import]
                    from core.clients import get_tasks_client as _get_tc  # type: ignore[import]
                    from google.protobuf import timestamp_pb2 as _ts_pb2  # type: ignore[import]
                    import datetime as _dt, json as _json
                    _tc = _get_tc()
                    _queue_path = _tc.queue_path(_pid, _loc, _q)
                    _when = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=3)
                    _ts = _ts_pb2.Timestamp()
                    _ts.FromDatetime(_when)
                    from core.config import ORCHESTRATOR_SA_EMAIL as _sa_email  # type: ignore[import]
                    _crm_retry_task = {
                        "schedule_time": _ts,
                        "http_request": {
                            "http_method": "POST",
                            "url": f"{_orch}/api/internal/crm-retry",
                            "body": _json.dumps({"lead_id": doc_id, "tenant_id": uid}).encode(),
                            "headers": {"Content-Type": "application/json"},
                        },
                    }
                    if _sa_email:
                        _crm_retry_task["http_request"]["oidc_token"] = {
                            "service_account_email": _sa_email,
                            "audience": _orch,
                        }
                    _tc.create_task(request={
                        "parent": _queue_path,
                        "task": _crm_retry_task,
                    })
                    log.info("crm_retry_task_enqueued", lead_id=doc_id, delay_hours=3)
                except Exception as _retry_err:
                    log.warning("crm_retry_enqueue_failed",
                                lead_id=doc_id, error=str(_retry_err))
                    crm_delivery_status = "failed_permanent"
            # Record delivery status on the lead document
            try:
                _db().collection("leads").document(doc_id).update(
                    {"crm_delivery_status": crm_delivery_status}
                )
            except Exception:
                pass

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
            # Query directly utilizing the composite index (tenant_id + status + intent_score desc)
            if not intent_label and not campaign_id:
                query = (
                    _db().collection("inbound_signals")
                    .where(filter=FieldFilter("tenant_id", "==", t_id))
                    .where(filter=FieldFilter("status", "==", status))
                    .order_by("intent_score", direction=fs.Query.DESCENDING)
                    .limit(limit)
                )
                signals = [_sanitize_signal_doc(d) for d in query.stream()]
            else:
                # If post-filters are active, read a larger slice to prevent premature slicing
                query = (
                    _db().collection("inbound_signals")
                    .where(filter=FieldFilter("tenant_id", "==", t_id))
                    .where(filter=FieldFilter("status", "==", status))
                    .order_by("intent_score", direction=fs.Query.DESCENDING)
                    .limit(300)
                )
                signals = [_sanitize_signal_doc(d) for d in query.stream()]
        except Exception as exc:
            log.warning("list_inbound_signals_index_query_failed_falling_back", error=str(exc))
            # Safe local fallback to original full-table memory filtering
            fallback_query = (
                _db().collection("inbound_signals")
                .where(filter=FieldFilter("tenant_id", "==", t_id))
            )
            signals = [_sanitize_signal_doc(d) for d in fallback_query.stream()]

        # Execute Application-Level Filtering / Validation
        # Floor 0.35 matches the domain-adjusted write-threshold floor used by
        # inbound_sentiment_job (base 0.45 ± domain strictness_bias). Prefer each
        # signal's intent_threshold_used when present so domain-lenient signals
        # are not silently dropped by a hard 0.45 list filter.
        def _passes_intent_floor(s: dict) -> bool:
            score = float(s.get("intent_score", 0.0) or 0.0)
            used = s.get("intent_threshold_used")
            if used is not None:
                try:
                    return score >= float(used)
                except (TypeError, ValueError):
                    pass
            return score >= 0.35

        signals = [
            s for s in signals
            if s.get("status") == status
            and _passes_intent_floor(s)
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
        log.exception("list_inbound_signals_fatal_ingress_error", error=str(e))
        return jsonify({"signals": [], "error": True}), 500


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
    if sig.get("tenant_id") != tenant_id:
        return jsonify({"error": "Forbidden"}), 403

    # Update signal status
    signal_ref.update({
        "status":     new_status,
        "updated_at": fs.SERVER_TIMESTAMP,
    })

    lead_id = None

    if new_status == "converted_to_lead":
        # FIN-02: Check wallet and consume a credit BEFORE creating the lead.
        # Previously credit_settled was set to True without any actual debit.
        # P1-FIN-1: Wrap credit check + increment in a Firestore transaction
        # to prevent race conditions where two concurrent signal conversions
        # both pass the check before either increments.
        from google.cloud.firestore_v1.transaction import transactional as _fs_txn

        @_fs_txn
        def _consume_signal_credit_txn(transaction, user_ref):
            """Transactional credit check + consume for signal-to-lead conversion."""
            _snap = user_ref.get(transaction=transaction)
            if not _snap.exists:
                raise ValueError("Tenant wallet document does not exist.")
            _w = (_snap.to_dict() or {}).get("wallet", {})
            _alloc = int(_w.get("allocated_credits", 0) or 0)
            _total_consumed = int(_w.get("total_consumed", 0) or 0)
            _legacy_consumed = int(_w.get("consumed_credits", 0) or 0)
            _reserved = int(_w.get("reserved_credits", 0) or 0)
            # Use max of both accounting paths (matches check_quota in helpers.py)
            _consumed = max(_total_consumed, _legacy_consumed)
            _avail = _alloc - _consumed - _reserved
            if _avail <= 0:
                raise ValueError("Insufficient credits to convert signal to lead")
            transaction.update(user_ref, {"wallet.total_consumed": fs.Increment(1)})

        try:
            _user_ref = _db().collection("users").document(tenant_id)
            _txn = _db().transaction()
            _consume_signal_credit_txn(_txn, _user_ref)
        except ValueError as _credit_err:
            log.warning("inbound_signal_conversion_insufficient_credits",
                        signal_id=signal_doc_id, tenant_id=tenant_id,
                        error=str(_credit_err))
            return jsonify({"error": str(_credit_err)}), 402
        except Exception as _credit_err:
            log.warning("inbound_signal_credit_check_failed", error=str(_credit_err),
                        note="Proceeding with conversion — credit check non-fatal.")

        # Promote signal to a full lead document
        lead_ref = _db().collection("leads").document()
        company  = sig.get("company_name") or "Unknown Company"
        lead_doc = {
            "uid":                uid,
            "tenant_id":          tenant_id,
            "url":                sig.get("source_url", ""),
            "company":            company,
            "company_name":       company,
            "summary":            sig.get("snippet", ""),
            "pain_point":         ", ".join(sig.get("pain_keywords", [])),
            "score":              round(sig.get("intent_score", 0.5) * 100),
            "normalized_score":   round(sig.get("intent_score", 0.5) * 100),
            "source":             "inbound_radar",
            "status":             "new",
            "is_in_crm":          False,
            "credit_settled":     True,  # FIN-02: Now set AFTER actual credit consumption above
            "fit_score":          sig.get("intent_score", 0.5),
            "intent_label":       sig.get("intent_label", "EXPRESSING_PAIN"),
            "inbound_signal_id":  sig.get("signal_id", ""),
            "inbound_platform":   sig.get("source_platform", "web"),
            "campaign_id":        sig.get("matched_campaign_id", ""),
            "matched_campaigns":  (
                [sig["matched_campaign_id"]]
                if sig.get("matched_campaign_id") else []
            ),
            "createdAt":          fs.SERVER_TIMESTAMP,
            "updatedAt":          fs.SERVER_TIMESTAMP,
        }
        # Propagate domain intelligence from signal (and campaign fallback).
        _domain_family = sig.get("domain_family")
        _domain_source = sig.get("domain_source")
        _profile_conf = sig.get("profile_confidence")
        if not _domain_family and sig.get("matched_campaign_id"):
            try:
                _camp_snap = (
                    _db().collection("campaigns")
                    .document(str(sig.get("matched_campaign_id")))
                    .get()
                )
                if _camp_snap.exists:
                    _dp = (_camp_snap.to_dict() or {}).get("system_domain_profile") or {}
                    if isinstance(_dp, dict) and _dp.get("domain_family"):
                        _domain_family = _dp.get("domain_family")
                        _domain_source = (
                            "domain_override"
                            if _dp.get("override_active")
                            else "system_domain_profile"
                        )
                        _profile_conf = _dp.get("profile_confidence")
                        if sig.get("thin_campaign") is None and _dp.get("thin_campaign") is not None:
                            lead_doc["thin_campaign"] = bool(_dp.get("thin_campaign"))
                        if sig.get("strictness_bias") is None and _dp.get("strictness_bias") is not None:
                            lead_doc["strictness_bias"] = _dp.get("strictness_bias")
            except Exception:
                pass
        if _domain_family:
            lead_doc["domain_family"] = _domain_family
            lead_doc["domain_source"] = _domain_source or "system_domain_profile"
        if _profile_conf:
            lead_doc["profile_confidence"] = _profile_conf
        if sig.get("thin_campaign") is not None:
            lead_doc["thin_campaign"] = bool(sig.get("thin_campaign"))
        if sig.get("strictness_bias") is not None:
            lead_doc["strictness_bias"] = sig.get("strictness_bias")
        if sig.get("intent_threshold_used") is not None:
            lead_doc["intent_threshold_used"] = sig.get("intent_threshold_used")
        for _ek in (
            "enrichment_priority",
            "enrichment_priority_rank",
            "enrichment_queue",
            "enrichment_resolve_company",
            "enrichment_max_lookups",
            "enrichment_score",
            "firmographic_value",
        ):
            if sig.get(_ek) is not None:
                lead_doc[_ek] = sig.get(_ek)

        lead_ref.set(lead_doc)
        lead_id = lead_ref.id
        log.info(
            "inbound_signal_converted_to_lead",
            uid=uid[:8],
            signal_id=sig.get("signal_id", ""),
            lead_id=lead_id,
            domain_family=lead_doc.get("domain_family"),
            domain_source=lead_doc.get("domain_source"),
            profile_confidence=lead_doc.get("profile_confidence"),
            enrichment_priority=lead_doc.get("enrichment_priority"),
            enrichment_queue=lead_doc.get("enrichment_queue"),
        )

    resp = {"status": new_status}
    if lead_id:
        resp["lead_id"] = lead_id
    return jsonify(resp), 200
