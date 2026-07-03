"""
Orchestrator V23 — Internal Webhooks Blueprint.

Cloud Task handlers and OIDC-protected cron endpoints. These are NOT
exposed to the public internet — they require either:
  - An X-CloudTasks-QueueName header (injected by Cloud Tasks)
  - A valid Google OIDC Bearer token (for cron routes)
  - An X-API-Key header (conversion_feedback)

Routes:
  POST /api/internal/telemetry/bq-push
  POST /api/internal/credits/settle
  POST /api/internal/cron/sweep
  POST /api/internal/cron/reflection
  POST /api/internal/cron/ontology-decay
  POST /api/telemetry/conversion_feedback
  POST /purge
"""
from __future__ import annotations

import datetime
import json

from flask import Blueprint, jsonify, request
from google.cloud.firestore_v1.base_query import FieldFilter

from core.clients import get_db  # type: ignore[import]
from core.config import PROJECT_ID, LOCATION, QUEUE, PIPELINE_URL, ORCHESTRATOR_SA_EMAIL  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]
from core.helpers import (  # type: ignore[import]
    _handle_bq_push_task,
    _atomic_settle_txn,
    check_quota,
    get_service_account_email,
    get_vector_weights,
    handle_purge,
)

try:
    from google.cloud import tasks_v2
    from google.protobuf import timestamp_pb2
    from google.cloud import bigquery
    from vertexai.generative_models import GenerativeModel, GenerationConfig  # type: ignore[import]
except ImportError as _imp_err:
    # These are critical runtime dependencies — a missing package will cause
    # NameError deep in route handlers.  Log at WARNING so the failure is
    # visible in GCP Cloud Logging instead of silently swallowed.
    import logging as _fallback_logging
    _fallback_logging.getLogger("orchestrator.v23.internal").warning(
        "critical_import_failed: %s — route handlers that depend on this "
        "package will fail with NameError at invocation time.", _imp_err,
    )

import random

# ---------------------------------------------------------------------------
# V23 Task 2 fix — NO module-level Firestore client instantiation.
#
# PREVIOUS BUG: ``db = get_db()`` at module scope (line 49 of legacy file)
# triggered firebase_admin.initialize_app() + firestore.Client() during
# Blueprint registration, BEFORE Gunicorn forked worker processes.  Child
# workers inherited an open gRPC channel in a copy-on-write page, causing
# mutex contention and indefinite hangs on the first real Firestore RPC.
#
# FIX: All route handlers call _db() which resolves lazily via get_db().
# The first call inside a live worker process is safe because
# _ensure_firebase_init() in core/clients.py guards initialisation with a
# threading.Lock.
# ---------------------------------------------------------------------------
def _db():
    """Return the shared Firestore client (lazy — never at import time)."""
    return get_db()


bp = Blueprint("internal", __name__)
log = get_logger("orchestrator.v23.internal")


def _verify_oidc(request_obj) -> tuple[bool, str]:
    """Return (is_valid, error_detail).

    V24.2: Validates OIDC token audience against ORCHESTRATOR_URL to prevent
    cross-service token replay attacks (OWASP A2:2021).
    """
    import os as _os
    auth = request_obj.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False, "Missing OIDC token"
    token = auth.split("Bearer ")[1]
    # Audience must match this service's own URL to prevent cross-service replay.
    # Fall back to None (no audience check) only if URL not configured — logged.
    from core.config import ORCHESTRATOR_URL as _orch_url  # type: ignore[import]
    expected_audience = _orch_url.strip() or None
    if not expected_audience:
        log.warning("oidc_audience_unconfigured",
                    note="ORCHESTRATOR_URL not set; OIDC audience validation skipped. "
                         "Set ORCHESTRATOR_URL to enable cross-service replay protection.")
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=expected_audience,
        )
        return True, ""
    except Exception as e:
        return False, str(e)


# =============================================================================
# POST /api/internal/telemetry/bq-push
# =============================================================================
@bp.route("/api/internal/telemetry/bq-push", methods=["POST"])
def bq_push():
    if not request.headers.get("X-CloudTasks-QueueName"):
        return jsonify({"error": "Forbidden — direct access not allowed"}), 403
    payload = request.json or {}
    if not payload.get("event_id") or not payload.get("tenant_id"):
        return jsonify({"error": "Invalid payload"}), 400
    success     = _handle_bq_push_task(payload)
    status_code = 200 if success else 500
    return jsonify({"ok": success}), status_code


# =============================================================================
# POST /api/internal/telemetry/serper-audit  (V23.4.2 — Schema + Error Exposure)
#
# Called by pipeline-main serper_service.py after every Serper API call.
# Unlike bq-push (which requires Cloud Tasks), this route accepts both:
#   - Direct OIDC Bearer token (from pipeline worker daemon thread)
#   - X-CloudTasks-QueueName header (if fired via Cloud Tasks in future)
#
# V23.4.2 fixes (2026-04-23):
#   BUG-1: Auth failures were returning 200 OK with {"ok": false} — the
#           pipeline worker's retry logic treats any non-5xx as success and
#           never retried. Auth failures now return 401 so the OIDC token
#           cache in serper_service.py triggers a refresh on the next call.
#   BUG-2: serper_parameters was json.dumps'd to a STRING before insertion.
#           BigQuery's JSON column type requires a native Python dict — the
#           SDK serialises it. Passing a pre-serialised string causes a schema
#           type mismatch that insert_rows_json reports as an error but the
#           old code swallowed (returned 200 OK regardless).
#   BUG-3: insert_rows_json errors were logged at WARNING and still returned
#           200 OK — making the worker believe the row was accepted. Errors
#           are now elevated to CRITICAL with the full BQ rejection payload
#           and return 500 so Cloud Tasks retries the delivery.
#   BUG-4: timestamp was formatted as a bare string "%Y-%m-%dT%H:%M:%SZ".
#           BQ TIMESTAMP NOT NULL columns require an ISO-8601 string with
#           explicit UTC suffix ("Z" or "+00:00"). We now ensure the format
#           is always correct and fall back to a freshly-minted UTC timestamp
#           if the incoming value is absent or malformed.
# =============================================================================
@bp.route("/api/internal/telemetry/serper-audit", methods=["POST"])
def serper_audit():
    """Receive one Serper query audit row and stream-insert it into BigQuery.

    Returns 200 only when the row is durably accepted by BigQuery.
    Returns 401 on OIDC auth failure (triggers worker token refresh + retry).
    Returns 500 on BQ schema rejection or SDK exception (triggers Cloud Tasks retry).
    """
    # ── Auth: Cloud Tasks header OR OIDC bearer ──────────────────────────────
    has_cloud_tasks = bool(request.headers.get("X-CloudTasks-QueueName"))
    if not has_cloud_tasks:
        ok, err = _verify_oidc(request)
        if not ok:
            # BUG-1 FIX: return 401, not 200. The pipeline worker's daemon thread
            # swallows all responses, but the OIDC token cache in serper_service.py
            # will refresh on the next run. Returning 200 previously masked every
            # auth failure as a successful insert.
            log.warning(
                "serper_audit_auth_failed",
                error=err,
                action="Returning 401. Worker OIDC cache will refresh on next call.",
            )
            return jsonify({"ok": False, "reason": "auth_failed"}), 401

    payload = request.json or {}
    if not payload.get("campaign_id") and not payload.get("tenant_id"):
        return jsonify({"error": "Missing campaign_id or tenant_id"}), 400

    try:
        from google.cloud import bigquery as _bq
        # REGIONALITY FIX: explicit location prevents default US routing (Code 5)
        bq        = _bq.Client(project=PROJECT_ID, location="asia-south1")
        table_ref = f"{PROJECT_ID}.swarm_analytics.serper_audit_logs"

        # ── Timestamp: must be strict ISO-8601 UTC string for BQ TIMESTAMP ──
        # BQ streaming insert accepts strings in RFC 3339 / ISO-8601 with UTC
        # offset. "%Y-%m-%dT%H:%M:%SZ" is correct but we validate the incoming
        # value and always fall back to a freshly-minted UTC timestamp.
        raw_ts = payload.get("timestamp") or ""
        if raw_ts and isinstance(raw_ts, str) and raw_ts.endswith("Z"):
            ts_value = raw_ts  # already RFC 3339 UTC
        else:
            # Re-format to a guaranteed-valid RFC 3339 string
            ts_value = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        # ── serper_parameters: SCHEMA FIX ─────────────────────────────────────
        # The BQ table column is typed JSON (DDL: serper_parameters JSON).
        # insert_rows_json() serialises the outer row to JSON, but for a JSON-
        # typed column the cell value must ITSELF be a JSON-encoded string —
        # i.e. the SDK expects a str produced by json.dumps(), NOT a native dict.
        # Passing a raw dict causes BQ to reject with a type-mismatch error
        # (the 500 confirmed in Cloud Logging on 2026-04-30).
        # Fix: always serialise the dict to a JSON string before handing it to BQ.
        raw_params  = payload.get("serper_parameters") or {}
        params_str  = json.dumps(raw_params if isinstance(raw_params, dict) else {})

        row = {
            "timestamp":          ts_value,
            "campaign_id":        str(payload.get("campaign_id") or ""),
            "tenant_id":          str(payload.get("tenant_id") or ""),
            "raw_query":          str(payload.get("raw_query") or ""),
            "serper_parameters":  params_str,           # JSON string → BQ JSON column
            "result_count":       int(payload.get("result_count") or 0),
            "credit_cost":        int(payload.get("credit_cost") or 1),
            "engine":             str(payload.get("engine") or "search"),
            "serper_status_code": int(payload.get("serper_status_code") or 200),
            "error_message":      payload.get("error_message") or None,
        }

        errors = bq.insert_rows_json(table_ref, [row])

        # ── BUG-3 FIX: Expose BQ schema rejections ───────────────────────────
        # insert_rows_json() returns a non-empty list when ANY row fails BQ
        # schema validation. The previous code logged at WARNING and still
        # returned 200 — making the BQ table permanently empty while every
        # response appeared successful.
        # Fix: CRITICAL log with the full rejection payload + 500 return so
        # Cloud Tasks retries the delivery.
        if errors:
            log.critical(
                "serper_audit_bq_schema_rejection",
                bq_errors=errors,          # full structured error from BQ SDK
                row_sent={
                    k: v for k, v in row.items()
                    if k not in ("raw_query",)  # omit long fields from log
                },
                table=table_ref,
                action="Returning 500 — Cloud Tasks will retry. Fix schema mismatch.",
            )
            return jsonify({
                "ok":     False,
                "errors": errors,          # exact BQ rejection payload to caller
            }), 500

        log.info(
            "serper_audit_row_inserted",
            campaign_id=row["campaign_id"][:12],
            tenant_id=row["tenant_id"][:12],
            engine=row["engine"],
            result_count=row["result_count"],
            timestamp=ts_value,
        )
        return jsonify({"ok": True}), 200

    except Exception as e:
        # SDK-level exception (network, auth, quota) — return 500 for retry
        log.critical(
            "serper_audit_bq_sdk_exception",
            error=str(e),
            error_type=type(e).__name__,
            action="Returning 500 — Cloud Tasks will retry.",
            exc_info=True,
        )
        return jsonify({"ok": False, "error": str(e)}), 500


# =============================================================================
# POST /api/internal/credits/settle
# =============================================================================
@bp.route("/api/internal/credits/settle", methods=["POST"])
def settle_credits():
    if not request.headers.get("X-CloudTasks-QueueName"):
        return jsonify({"error": "Forbidden"}), 403
    payload   = request.json or {}
    tenant_id = payload.get("tenant_id")
    outcome   = payload.get("outcome")
    count     = int(payload.get("count", 1))
    lead_id   = payload.get("lead_id", "")
    if not tenant_id or outcome not in ("success", "failure") or count <= 0:
        return jsonify({"error": "Invalid settlement payload"}), 400
    user_ref = _db().collection("users").document(tenant_id)
    lead_ref = _db().collection("leads").document(lead_id) if lead_id else None
    try:
        txn = _db().transaction()
        _atomic_settle_txn(txn, user_ref, lead_ref, outcome, count)
        log.info("credit_settled", tenant_id=tenant_id, outcome=outcome, count=count,
                 lead_id=lead_id[:12] if lead_id else "N/A")
    except ValueError as ve:
        if "already_settled" in str(ve):
            return jsonify({"ok": True, "outcome": "already_settled"}), 200
        return jsonify({"ok": False, "error": str(ve)}), 500
    except Exception as se:
        return jsonify({"ok": False, "error": str(se)}), 500
    return jsonify({"ok": True, "outcome": outcome, "count": count}), 200


# =============================================================================
# POST /purge
# =============================================================================
@bp.route("/purge", methods=["POST"])
def purge():
    ok, err = _verify_oidc(request)
    if not ok:
        return jsonify({"error": err}), 401 if "Missing" in err else 403
    return handle_purge(request)


# =============================================================================
# POST /api/internal/cron/sweep
# =============================================================================
@bp.route("/api/internal/cron/sweep", methods=["POST"])
def cron_sweep():
    from google.cloud import tasks_v2 as tv2
    from google.protobuf import timestamp_pb2 as ts_pb2

    ok, err = _verify_oidc(request)
    if not ok:
        return jsonify({"error": err}), 401 if "Missing" in err else 403

    # ── Kill Switch Gate (V24.1) ──────────────────────────────────────────────
    # If the admin has activated the global kill switch via POST /api/l0/kill-switch,
    # skip ALL campaign processing and return 200 (so Cloud Scheduler doesn't retry).
    try:
        _ks_doc = _db().collection("system_telemetry").document("kill_switch").get()
        _ks_data = _ks_doc.to_dict() or {} if _ks_doc.exists else {}
        if _ks_data.get("active") is True:
            log.info(
                "kill_switch_active",
                activated_by=_ks_data.get("activated_by", "unknown"),
                activated_at=str(_ks_data.get("activated_at", "")),
                message="Sweep aborted — kill switch is engaged.",
            )
            return jsonify({
                "message": "Kill switch active — sweep skipped.",
                "kill_switch_active": True,
                "activated_by": _ks_data.get("activated_by", "unknown"),
            }), 200
    except Exception as _ks_err:
        # Fail-open: if we can't read the kill switch, proceed with sweep
        log.warning("kill_switch_read_failed", error=str(_ks_err),
                    fallback="Proceeding with sweep despite kill-switch read failure.")

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    # ── Task 1: OIDC config pre-flight ────────────────────────────────────────
    # Fail fast with HTTP 412 (Precondition Failed) — NOT 503.
    # Rationale: 503 triggers Cloud Scheduler exponential-backoff retry storms
    # when the env var is permanently missing (a configuration error, not a
    # transient one).  412 causes Cloud Scheduler to record a hard failure and
    # stop retrying, alerting the operator immediately.
    if not ORCHESTRATOR_SA_EMAIL:
        log.critical(
            "oidc_sa_email_missing_hard_fail",
            action="Sweep aborted — set ORCHESTRATOR_SA_EMAIL env var in Cloud Run console.",
            impact="All pipeline Cloud Tasks will be dispatched WITHOUT an OIDC token "
                   "and will be rejected with HTTP 403 by Cloud Run IAM.",
            resolution="Cloud Run → Edit & Deploy → Environment Variables → "
                        "ORCHESTRATOR_SA_EMAIL=<service-account>@<project>.iam.gserviceaccount.com",
        )
        return jsonify({
            "error": "Precondition Failed",
            "code": "ORCHESTRATOR_SA_EMAIL_MISSING",
            "message": "ORCHESTRATOR_SA_EMAIL environment variable is not set. "
                       "Set it in Cloud Run to enable OIDC task authentication.",
        }), 412

    base_url = PIPELINE_URL.split("/dispatch")[0].strip()
    if not base_url:
        log.critical(
            "pipeline_url_missing_hard_fail",
            action="Sweep aborted — PIPELINE_URL env var is empty or malformed.",
            resolution="Set PIPELINE_URL to the pipeline-main Cloud Run base URL.",
        )
        return jsonify({
            "error": "Precondition Failed",
            "code": "PIPELINE_URL_MISSING",
            "message": "PIPELINE_URL is not set or does not contain a valid base URL.",
        }), 412

    sa_email = ORCHESTRATOR_SA_EMAIL
    log.info(
        "oidc_sa_email_from_config",
        sa_email=sa_email,
        oidc_audience=base_url,
        produce_url=f"{base_url}/produce",
        dispatch_url=f"{base_url}/dispatch",
        note="OIDC token audience must exactly match PIPELINE_MAIN_URL on the worker. "
             "Mismatch causes HTTP 401 from zero-trust middleware.",
    )

    # ── Task 4: Circuit Breaker — per-vector isolation ────────────────────────
    # Read breaker state once. Build a set of blocked sourcing vectors.
    # GeneralDomain (Serper + scraper) is affected by both thresholds.
    # WalledGarden (LinkedIn, social-platform) is immune to Serper/scraper.
    # Per-campaign gate (below) skips only campaigns whose vector is blocked;
    # all others proceed normally.
    SERPER_429_THRESHOLD  = float(__import__("os").environ.get("CB_SERPER_THRESHOLD",  "0.15"))
    SCRAPER_OOM_THRESHOLD = float(__import__("os").environ.get("CB_SCRAPER_THRESHOLD", "0.05"))
    CB_WINDOW_MINUTES     = int(__import__("os").environ.get("CB_WINDOW_MINUTES", "15"))

    # Mapping: which sourcing vector labels are impacted by each breaker.
    # FIX (2026-06-21): Updated to archetype-based vectors.
    # GeneralDomain vectors depend on Serper + scraper-heavy.
    # Consumer archetypes (B2C, D2C) are immune to Serper circuit breakers.
    _GENERAL_DOMAIN_VECTORS = frozenset({
        "B2B", "Classic B2B", "Review Hijacking", "Maps/GMB Targeting",
    })
    _WALLED_GARDEN_VECTORS = frozenset({
        "Social/Forum Listening",
    })

    blocked_vectors: set[str] = set()
    serper_rate = scraper_rate = 0.0
    try:
        cb_data = (_db().collection("system_telemetry").document("circuit_breaker_state").get().to_dict() or {})
        window_start  = now_utc - datetime.timedelta(minutes=CB_WINDOW_MINUTES)
        serper_total  = int(cb_data.get("serper_calls_window",  0))
        serper_429s   = int(cb_data.get("serper_429s_window",   0))
        scraper_total = int(cb_data.get("scraper_calls_window", 0))
        scraper_ooms  = int(cb_data.get("scraper_ooms_window",  0))
        window_reset  = cb_data.get("window_reset_at")
        if window_reset:
            if hasattr(window_reset, "tzinfo") and window_reset.tzinfo is None:
                window_reset = window_reset.replace(tzinfo=datetime.timezone.utc)
            if window_reset < window_start:
                serper_total = serper_429s = scraper_total = scraper_ooms = 0
        serper_rate  = (serper_429s  / serper_total)  if serper_total  > 10 else 0.0
        scraper_rate = (scraper_ooms / scraper_total) if scraper_total > 10 else 0.0
        if serper_rate > SERPER_429_THRESHOLD or scraper_rate > SCRAPER_OOM_THRESHOLD:
            blocked_vectors.update(_GENERAL_DOMAIN_VECTORS)
            # V23 Task 4 Amendment: structured warning for GCP Log-Based alert.
            log.warning(
                "circuit_breaker_active",
                blocked_vectors=sorted(blocked_vectors),
                immune_vectors=sorted(_WALLED_GARDEN_VECTORS),
                status="partial_sweep",
                serper_rate_pct=round(serper_rate * 100, 2),
                scraper_rate_pct=round(scraper_rate * 100, 2),
                serper_threshold_pct=round(SERPER_429_THRESHOLD * 100, 2),
                scraper_threshold_pct=round(SCRAPER_OOM_THRESHOLD * 100, 2),
                note="Wire a GCP Log-Based Metric on jsonPayload.message=circuit_breaker_active "
                     "to preserve 5xx-equivalent alerting.",
            )
    except Exception as cb_err:
        log.warning("circuit_breaker_read_failed_fail_open", error=str(cb_err))

    # ── Main sweep ───────────────────────────────────────────────────────────
    campaigns = list(
        _db().collection("campaigns")
          .where(filter=FieldFilter("status", "==", "active"))
          .limit(500)
          .stream()
    )
    # DIAGNOSTIC: always visible in Cloud Logging — confirms query executed
    log.info("sweep_query_executed",
             campaign_count=len(campaigns),
             blocked_vectors=sorted(blocked_vectors) if blocked_vectors else [],
             note="Only status==active filter applied. No secondary filters.")

    audit_trail   = [f"Executed V23 Dual-Mode Sweep. Found {len(campaigns)} active campaigns."]
    if blocked_vectors:
        audit_trail.append(
            f"⚡ Circuit breaker ACTIVE: {sorted(blocked_vectors)} blocked. "
            f"Serper={serper_rate*100:.1f}% | Scraper={scraper_rate*100:.1f}%"
        )
    # SF-009 FIX: Use DCL singleton instead of constructing a new CloudTasksClient
    # on every sweep invocation. New clients = new gRPC channels = FD leak.
    # After 144 sweeps (12h), the container's FD limit (~1024) is breached and
    # create_task() fails with OSError: Too many open files.
    from core.clients import get_tasks_client as _get_tasks_client
    tasks_client = _get_tasks_client()
    queue_path   = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)

    produce_url = f"{base_url}/produce"
    consume_url = f"{base_url}/dispatch"
    produce_dispatched = consume_dispatched = 0

    def _oidc_task(url, payload):
        t: dict = {
            "http_request": {
                "http_method": tv2.HttpMethod.POST,
                "url": url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode(),
            }
        }
        # sa_email is guaranteed non-empty at this point (412 guard above).
        t["http_request"]["oidc_token"] = {
            "service_account_email": sa_email,
            "audience": base_url,  # audience = pipeline-main base URL
        }
        return t

    # Postmortem Fix #5: Pre-fetch all user docs in ONE batch call before the loop.
    # Previous: sequential _db().collection("users").document(tid).get() per campaign
    # = N × 20ms blocking Firestore reads inside the HTTP handler.
    # 500 campaigns × 20ms = 10s sequential I/O → Cloud Run 60s timeout → campaigns silently skipped.
    # Fix: build a list of DocumentReferences and use db.get_all() for one bulk RPC.
    _db_instance = _db()
    user_refs    = {}
    for camp_doc in campaigns:
        tid = (camp_doc.to_dict() or {}).get("tenant_id")
        if tid and tid not in user_refs:
            user_refs[tid] = _db_instance.collection("users").document(tid)
    user_docs_map: dict = {}
    if user_refs:
        try:
            fetched = _db_instance.get_all(list(user_refs.values()))
            for snap in fetched:
                user_docs_map[snap.id] = snap.to_dict() or {}
        except Exception as batch_err:
            log.warning("sweep_batch_user_fetch_failed", error=str(batch_err),
                        note="Falling back to per-campaign reads for this sweep.")

    for camp_doc in campaigns:
        campaign_data = camp_doc.to_dict() or {}
        campaign_id   = camp_doc.id
        tenant_id     = campaign_data.get("tenant_id")

        log.info("sweep_campaign_evaluating", campaign_id=campaign_id,
                 status=campaign_data.get("status"),
                 next_produce_due=str(campaign_data.get("next_produce_due")),
                 next_drip_due=str(campaign_data.get("next_drip_due")))

        if not tenant_id:
            log.warning("BYPASS_NO_TENANT_ID",
                        campaign_id=campaign_id,
                        reason="campaign document missing tenant_id field — skipping")
            audit_trail.append(f"⚠️ SKIPPED {campaign_id}: missing tenant_id field")
            continue

        # ── Task 4: Per-vector circuit breaker gate ──────────────────────────────
        # Skip only campaigns whose sourcing vector is in the blocked set.
        # WalledGarden campaigns (Social/Forum Listening) are immune and
        # continue even when Serper / scraper thresholds are breached.
        campaign_vector = campaign_data.get("sourcing_vector", "B2B") or "B2B"
        if campaign_vector in blocked_vectors:
            log.warning(
                "circuit_breaker_campaign_skipped",
                campaign_id=campaign_id,
                tenant_id=tenant_id,
                sourcing_vector=campaign_vector,
                blocked_vectors=sorted(blocked_vectors),
                status="skipped_by_circuit_breaker",
            )
            audit_trail.append(
                f"⚡ CIRCUIT-BREAKER SKIP {campaign_id} "
                f"(vector={campaign_vector}): GeneralDomain blocked"
            )
            continue

        # ── Quota gate ───────────────────────────────────────────────────────
        # Postmortem Fix #5: use the pre-fetched user_docs_map instead of a
        # per-iteration Firestore read. Falls back to a live read if the batch
        # fetch failed for this tenant (non-fatal degradation).
        if tenant_id in user_docs_map:
            user_doc = user_docs_map[tenant_id]
        else:
            # Fallback path (batch read failed or new tenant added after batch)
            user_doc = (_db_instance.collection("users").document(tenant_id).get().to_dict() or {})
        user_role = user_doc.get("role", "")
        if user_role != "super_admin":
            wallet    = user_doc.get("wallet", {})
            # FIELD SCHEMA NOTE:
            #   allocated_credits — total credits granted (always present)
            #   total_consumed    — authoritative consumed count (written by _atomic_settle_txn)
            #   consumed_credits  — legacy field (written by old wallet_shards path)
            #   reserved_credits  — in-flight reservations
            # We read BOTH consumed field names and take the max to handle schema drift.
            credits   = int(wallet.get("allocated_credits", 0) or 0)
            consumed  = max(
                int(wallet.get("total_consumed",   0) or 0),
                int(wallet.get("consumed_credits", 0) or 0),
            )
            reserved  = int(wallet.get("reserved_credits", 0) or 0)
            available = credits - consumed - reserved
            log.info("sweep_quota_check",
                     campaign_id=campaign_id, tenant_id=tenant_id,
                     credits=credits, consumed=consumed,
                     reserved=reserved, available=available)
            if available <= 0:
                log.warning("BYPASS_QUOTA_EXHAUSTED",
                            campaign_id=campaign_id, tenant_id=tenant_id,
                            credits=credits, consumed=consumed,
                            reserved=reserved, available=available,
                            reason="available credits <= 0 — no task dispatched")
                audit_trail.append(f"🚫 SKIPPED {campaign_id} (tenant={tenant_id[:8]}): quota exhausted "
                                   f"[allocated={credits} consumed={consumed} reserved={reserved}]")
                continue

        # ── Producer (24h interval) ──────────────────────────────────────────
        PRODUCE_INTERVAL_H = 24
        next_produce_due   = campaign_data.get("next_produce_due")
        produce_due        = True

        # next_produce_due may be:
        #   (a) a Firestore DatetimeWithNanoseconds  → hasattr(.timestamp) = True
        #   (b) an ISO-8601 string "2026-..."        → written by our finally: block
        #   (c) None                                 → field missing, treat as overdue
        if next_produce_due:
            ndd_dt = None
            if hasattr(next_produce_due, "timestamp"):
                # Firestore native datetime
                ndd_dt = next_produce_due
                if hasattr(ndd_dt, "tzinfo") and ndd_dt.tzinfo is None:
                    ndd_dt = ndd_dt.replace(tzinfo=datetime.timezone.utc)
            elif isinstance(next_produce_due, str):
                # ISO-8601 string written by our finally: block
                try:
                    ndd_dt = datetime.datetime.fromisoformat(next_produce_due)
                    if ndd_dt.tzinfo is None:
                        ndd_dt = ndd_dt.replace(tzinfo=datetime.timezone.utc)
                except Exception as _parse_err:
                    log.warning("BYPASS_PRODUCE_TIMESTAMP_UNPARSEABLE",
                                campaign_id=campaign_id,
                                next_produce_due=str(next_produce_due),
                                error=str(_parse_err),
                                action="Treating as overdue — produce_due=True")
            if ndd_dt is not None:
                try:
                    if ndd_dt > now_utc:
                        produce_due = False
                        log.info("BYPASS_PRODUCE_NOT_YET_DUE",
                                 campaign_id=campaign_id,
                                 next_produce_due=str(next_produce_due),
                                 now_utc=now_utc.isoformat(),
                                 hours_remaining=round((ndd_dt - now_utc).total_seconds() / 3600, 2))
                except Exception as _cmp_err:
                    log.warning("BYPASS_PRODUCE_TIMESTAMP_CMP_FAILED",
                                campaign_id=campaign_id, error=str(_cmp_err),
                                action="Treating as overdue")

        log.info("sweep_produce_gate_result",
                 campaign_id=campaign_id, produce_due=produce_due,
                 next_produce_due=str(next_produce_due))

        if produce_due:
            _produce_ok = False  # tracks whether create_task() succeeded
            try:
                jitter  = random.randint(1, 120)
                sched_t = ts_pb2.Timestamp()
                sched_t.FromDatetime(now_utc + datetime.timedelta(seconds=jitter))
                task    = _oidc_task(produce_url, {"tenant_id": tenant_id, "campaign_id": campaign_id})
                task["schedule_time"] = sched_t
                # Postmortem Fix #9: cap Cloud Tasks retries to prevent thundering-herd
                # retry storms on container cold-starts. Without this, all 1000 tasks in
                # queue retry simultaneously after a cold-start 5xx, overwhelming the
                # single Cloud Run instance (1 vCPU × 8 threads).
                task["dispatch_deadline"] = {"seconds": 300}  # 5-minute max per task attempt
                tasks_client.create_task(request={
                    "parent": queue_path,
                    "task":   task,
                })
                _produce_ok = True
                produce_dispatched += 1
                log.info("produce_task_dispatched",
                         campaign_id=campaign_id, tenant_id=tenant_id, jitter_s=jitter)
                audit_trail.append(f"🏭 PRODUCER queued for {campaign_id} (jitter={jitter}s)")
            except Exception as prod_err:
                # ── FORENSIC: structured error — triggers GCP Log-Based alert ──────────
                # Clock is intentionally NOT advanced here. The campaign will be
                # retried on the next 5-minute sweep cycle (Architecture S5.4 amendment).
                log.error(
                    "producer_dispatch_failed",
                    campaign_id=campaign_id,
                    tenant_id=tenant_id,
                    error=str(prod_err),
                    exc_info=True,
                    oidc_audience=base_url,
                    produce_url=produce_url,
                    sa_email=sa_email,
                    action="Clock NOT advanced. Campaign will retry on next sweep.",
                )
                audit_trail.append(f"❌ PRODUCER ERROR {campaign_id}: {prod_err}")

            # ── Clock advance: ONLY on successful dispatch ────────────────────────────
            # ARCHITECTURE FIX (S5.4 — 2026-04-18):
            #   Previously a finally: block advanced next_produce_due unconditionally.
            #   Bug: if Cloud Tasks API returned PermissionDenied / InvalidArgument /
            #   network timeout, the clock still advanced, permanently skipping the
            #   next 24-hour production cycle (silent data loss window).
            #
            #   Fix: _produce_ok gates the write. On failure the clock is held at its
            #   current value. The next 5-minute sweep re-evaluates produce_due and
            #   retries dispatch. The campaign is NEVER silently skipped.
            if _produce_ok:
                try:
                    _next_produce = (now_utc + datetime.timedelta(hours=PRODUCE_INTERVAL_H)).isoformat()
                    camp_doc.reference.update({"next_produce_due": _next_produce})
                    log.info("produce_clock_advanced", campaign_id=campaign_id,
                             next_produce_due=_next_produce)
                except Exception as ts_err:
                    log.error("produce_timestamp_update_failed", campaign_id=campaign_id,
                              error=str(ts_err), exc_info=True)
            else:
                log.warning(
                    "produce_clock_held",
                    campaign_id=campaign_id,
                    reason="task dispatch failed — clock intentionally NOT advanced",
                    action="Campaign will be retried by the next scheduled sweep.",
                )
        # ── Consumer (drip interval) ──────────────────────────────────────────
        try:
            drip_interval_mins = int(campaign_data.get("drip_interval_minutes") or 240)
            if drip_interval_mins <= 0:
                drip_interval_mins = 240
        except Exception:
            drip_interval_mins = 240

        # CRITICAL: the entire drip evaluation MUST live inside try/finally.
        # Any exception in the timestamp comparison phase (e.g. TypeError from
        # naive vs aware datetime, AttributeError on malformed Firestore
        # DatetimeWithNanoseconds) would otherwise crash BETWEEN the produce
        # finally: and this finally:, permanently skipping next_drip_due update.
        try:
            next_drip_due = campaign_data.get("next_drip_due")
            drip_due      = True

            if next_drip_due:
                ndd_drip = None
                if hasattr(next_drip_due, "timestamp"):
                    ndd_drip = next_drip_due
                    if hasattr(ndd_drip, "tzinfo") and ndd_drip.tzinfo is None:
                        ndd_drip = ndd_drip.replace(tzinfo=datetime.timezone.utc)
                elif isinstance(next_drip_due, str):
                    try:
                        ndd_drip = datetime.datetime.fromisoformat(next_drip_due)
                        if ndd_drip.tzinfo is None:
                            ndd_drip = ndd_drip.replace(tzinfo=datetime.timezone.utc)
                    except Exception as _dp_err:
                        log.warning("drip_timestamp_unparseable",
                                    campaign_id=campaign_id,
                                    next_drip_due=str(next_drip_due),
                                    error=str(_dp_err))
                if ndd_drip is not None:
                    try:
                        if ndd_drip > now_utc:
                            drip_due = False
                            log.info("BYPASS_DRIP_NOT_YET_DUE",
                                     campaign_id=campaign_id,
                                     next_drip_due=str(next_drip_due),
                                     hours_remaining=round((ndd_drip - now_utc).total_seconds() / 3600, 2))
                    except Exception as ts_cmp_err:
                        # Malformed timestamp — treat as overdue so drip fires
                        log.warning("drip_timestamp_comparison_failed",
                                    campaign_id=campaign_id, error=str(ts_cmp_err))
                        drip_due = True

            queue_depth = len(campaign_data.get("unprocessed_queue", []) or [])

            log.info("sweep_drip_gate_result",
                     campaign_id=campaign_id, drip_due=drip_due,
                     queue_depth=queue_depth)

            if drip_due:
                if queue_depth == 0:
                    log.info("BYPASS_DRIP_QUEUE_EMPTY",
                             campaign_id=campaign_id,
                             reason="unprocessed_queue is empty — consumer skipped, clock will advance")
                    audit_trail.append(
                        f"⏸ CONSUMER skipped {campaign_id}: queue empty — drip timer advancing"
                    )
                    # Queue is empty — safe to advance clock (no task was dispatched,
                    # so there is nothing to lose by advancing to the next interval).
                    _dispatch_ok = True  # treat empty-queue skip as success for clock gate
                else:
                    _dispatch_ok = False  # tracks whether create_task() succeeded
                    try:
                        jitter  = random.randint(1, 290)
                        sched_t = ts_pb2.Timestamp()
                        sched_t.FromDatetime(now_utc + datetime.timedelta(seconds=jitter))
                        task    = _oidc_task(consume_url, {"tenant_id": tenant_id, "campaign_id": campaign_id})
                        task["schedule_time"] = sched_t
                        task["dispatch_deadline"] = {"seconds": 300}
                        tasks_client.create_task(request={"parent": queue_path, "task": task})
                        _dispatch_ok = True
                        consume_dispatched += 1
                        log.info("consume_task_dispatched",
                                 campaign_id=campaign_id, tenant_id=tenant_id,
                                 queue_depth=queue_depth, jitter_s=jitter)
                        audit_trail.append(f"⚙️ CONSUMER queued for {campaign_id} (depth={queue_depth})")
                    except Exception as drip_task_err:
                        # ── S5.4 AMENDMENT (consumer path — 2026-04-19) ─────────────────
                        # Previously: finally: block advanced next_drip_due unconditionally.
                        # Bug: Cloud Tasks 429/503/PermissionDenied → clock advanced anyway
                        # → 4-hour silent data loss window (mirror of producer bug S5.4).
                        # Fix: _dispatch_ok=False holds the clock. Campaign retries in 5 min.
                        log.error(
                            "consumer_dispatch_failed",
                            campaign_id=campaign_id,
                            tenant_id=tenant_id,
                            error=str(drip_task_err),
                            exc_info=True,
                            consume_url=consume_url,
                            queue_depth=queue_depth,
                            action="Clock NOT advanced. Campaign will retry on next sweep.",
                        )
                        audit_trail.append(f"❌ CONSUMER DISPATCH ERROR {campaign_id}: {drip_task_err}")

            else:
                _dispatch_ok = False  # drip not due — clock must not advance

        except Exception as drip_err:
            _dispatch_ok = False
            log.error("consumer_sweep_error", campaign_id=campaign_id,
                      tenant_id=tenant_id, error=str(drip_err), exc_info=True)
            audit_trail.append(f"❌ CONSUMER SWEEP ERROR {campaign_id}: {drip_err}")

        # ── Clock advance: ONLY on successful dispatch (S5.4 consumer amendment) ──
        # ARCHITECTURE FIX (S5.4 — consumer path — 2026-04-19):
        #   Previously a finally: block advanced next_drip_due unconditionally,
        #   identical to the producer bug fixed in commit f714b3c.
        #   Fix: _dispatch_ok gates the write. On failure, clock is held at its
        #   current value — the next 5-minute sweep retries dispatch.
        if drip_due and _dispatch_ok:
            try:
                _next_drip = (now_utc + datetime.timedelta(minutes=drip_interval_mins)).isoformat()
                camp_doc.reference.update({
                    "next_drip_due":         _next_drip,
                    "drip_interval_minutes": drip_interval_mins,
                })
                log.info("drip_clock_advanced", campaign_id=campaign_id,
                         next_drip_due=_next_drip)
            except Exception as ts_err:
                log.error("drip_timestamp_update_failed", campaign_id=campaign_id,
                          error=str(ts_err), exc_info=True)
        elif drip_due and not _dispatch_ok:
            log.warning(
                "drip_clock_held",
                campaign_id=campaign_id,
                reason="consumer task dispatch failed — clock intentionally NOT advanced",
                action="Campaign will be retried by the next scheduled sweep.",
            )


    # ── Zombie Lead Recovery (V23.7 — Retry Escalation Topology) ────────────
    from google.cloud import firestore as _fs
    ZOMBIE_MINS    = 15
    MAX_RETRIES    = 3
    zombie_cutoff  = now_utc - datetime.timedelta(minutes=ZOMBIE_MINS)
    zombie_recovered = zombie_locks_released = zombie_dlq = 0
    try:
        zombie_docs = (
            _db().collection("leads")
              .where(filter=FieldFilter("status",    "==", "processing"))
              .where(filter=FieldFilter("processing_started_at", "<",  zombie_cutoff))
              .limit(100).stream()
        )
        for zombie_doc in zombie_docs:
            zombie_data = zombie_doc.to_dict() or {}
            current_retry = zombie_data.get("retry_count", 0)
            try:
                if current_retry < MAX_RETRIES:
                    # ── AUTO-RETRY: re-queue with incremented counter ──
                    zombie_doc.reference.update({
                        "status":       "queued",
                        "lock_entity":  None,
                        "error":        None,
                        "retry_count":  current_retry + 1,
                        "recovered_at": _fs.SERVER_TIMESTAMP,
                    })
                    zombie_recovered += 1
                    log.info("zombie_auto_retry",
                             doc_id=zombie_doc.id,
                             retry_attempt=current_retry + 1,
                             max_retries=MAX_RETRIES,
                             note=f"Zombie recovery triggered auto-retry attempt "
                                  f"{current_retry + 1} for doc {zombie_doc.id}")
                    # Dispatch fresh Cloud Task for re-processing
                    try:
                        _zombie_tenant = zombie_data.get("tenant_id", "")
                        _zombie_camp   = zombie_data.get("campaign_id", "")
                        if _zombie_tenant and _zombie_camp:
                            _z_jitter  = random.randint(5, 60)
                            _z_sched   = ts_pb2.Timestamp()
                            _z_sched.FromDatetime(now_utc + datetime.timedelta(seconds=_z_jitter))
                            _z_task    = _oidc_task(consume_url, {
                                "tenant_id": _zombie_tenant,
                                "campaign_id": _zombie_camp,
                            })
                            _z_task["schedule_time"]     = _z_sched
                            _z_task["dispatch_deadline"] = {"seconds": 300}
                            tasks_client.create_task(request={
                                "parent": queue_path, "task": _z_task,
                            })
                            log.info("zombie_retry_task_dispatched",
                                     doc_id=zombie_doc.id,
                                     jitter_s=_z_jitter)
                    except Exception as zt_err:
                        log.warning("zombie_retry_task_dispatch_failed",
                                    doc_id=zombie_doc.id, error=str(zt_err))
                else:
                    # ── TERMINAL: max retries exhausted → DLQ ──
                    zombie_doc.reference.update({
                        "status":       "failed",
                        "lock_entity":  None,
                        "error":        f"Zombie recovery failed after {MAX_RETRIES} retries "
                                        f"(>{ZOMBIE_MINS}min execution limit).",
                        "recovered_at": _fs.SERVER_TIMESTAMP,
                    })
                    zombie_dlq += 1
                    log.warning("zombie_terminal_failure_dlq",
                                doc_id=zombie_doc.id,
                                retry_count=current_retry,
                                note="Max retries exhausted. Routed to dead_letter_leads.")
                    # Route payload to Dead Letter Queue collection
                    try:
                        _dlq_payload = dict(zombie_data)
                        _dlq_payload["original_doc_id"]  = zombie_doc.id
                        _dlq_payload["dlq_reason"]        = f"Zombie recovery exhausted {MAX_RETRIES} retries"
                        _dlq_payload["dlq_routed_at"]     = _fs.SERVER_TIMESTAMP
                        _db().collection("dead_letter_leads").add(_dlq_payload)
                    except Exception as dlq_err:
                        log.error("zombie_dlq_write_failed",
                                  doc_id=zombie_doc.id, error=str(dlq_err))
                    # Release lock + refund credit only on terminal failure
                    lock_entity = zombie_data.get("lock_entity")
                    if lock_entity:
                        try:
                            lr = _db().collection("global_lead_locks").document(lock_entity)
                            if lr.get().exists:
                                lr.delete()
                                zombie_locks_released += 1
                        except Exception as _lock_del_err:
                            log.error("zombie_lock_release_failed",
                                      doc_id=zombie_doc.id,
                                      lock_entity=lock_entity,
                                      error=str(_lock_del_err))
                    zombie_tenant = zombie_data.get("tenant_id")
                    if zombie_tenant:
                        try:
                            _db().collection("users").document(zombie_tenant).update(
                                {"wallet.reserved_credits": _fs.Increment(-1)}
                            )
                        except Exception as _credit_refund_err:
                            log.error("zombie_credit_refund_failed",
                                      doc_id=zombie_doc.id,
                                      tenant_id=zombie_tenant,
                                      error=str(_credit_refund_err))
            except Exception as _zombie_doc_err:
                log.warning("zombie_doc_recovery_failed",
                            doc_id=zombie_doc.id if hasattr(zombie_doc, 'id') else 'unknown',
                            error=str(_zombie_doc_err))
                continue
    except Exception as zse:
        audit_trail.append(f"⚠️ Zombie sweep error: {zse}")

    log.info("cron_sweep_complete",
             producers=produce_dispatched, consumers=consume_dispatched,
             zombies=zombie_recovered, zombie_dlq=zombie_dlq,
             circuit_breaker_blocked_vectors=sorted(blocked_vectors) if blocked_vectors else [])
    return jsonify({
        "message": f"V23 Sweep: {produce_dispatched} producers + {consume_dispatched} consumers.",
        "produce_dispatched":    produce_dispatched,
        "consume_dispatched":    consume_dispatched,
        "zombie_recovered":      zombie_recovered,
        "zombie_locks_released": zombie_locks_released,
        "zombie_dlq":            zombie_dlq,
        "circuit_breaker":       "partial" if blocked_vectors else "closed",
        "blocked_vectors":       sorted(blocked_vectors) if blocked_vectors else [],
        "audit_trail":           audit_trail,
    }), 200


# =============================================================================
# POST /api/internal/cron/harvest-sweep  (V25.2.0)
# Called by Cloud Scheduler every 4 hours (offset by 2h from /cron/sweep).
# Fans out one /harvest Cloud Task per active campaign — no Serper, no QueryBrain.
# =============================================================================
@bp.route("/api/internal/cron/harvest-sweep", methods=["POST"])
def cron_harvest_sweep():
    """V25.2.0 — Signal harvest fan-out sweep (4-hour cadence).

    Identical fan-out pattern to cron_sweep(), but targets /harvest
    instead of /produce. Does NOT run Serper or QueryBrain — signal
    harvest sources (Reddit, classified, forum, Google Reviews, YouTube)
    only. Allows fresh signals between 6-hour Serper sweeps.

    Guards applied:
      - OIDC verification (same as all cron routes)
      - Kill switch gate (shared Firestore state with cron_sweep)
      - ORCHESTRATOR_SA_EMAIL + PIPELINE_URL pre-flight
      - Credit gate per tenant (skip if remaining_credits <= 0)

    Jitter: 0–60s per task (distinct from /produce 0–120s jitter).
    """
    from google.cloud import tasks_v2 as tv2
    import random as _random

    ok, err = _verify_oidc(request)
    if not ok:
        return jsonify({"error": err}), 401 if "Missing" in err else 403

    # Kill switch gate (shared with cron_sweep)
    try:
        _ks_doc  = _db().collection("system_telemetry").document("kill_switch").get()
        _ks_data = _ks_doc.to_dict() or {} if _ks_doc.exists else {}
        if _ks_data.get("active") is True:
            log.info(
                "harvest_sweep_kill_switch_active",
                activated_by=_ks_data.get("activated_by", "unknown"),
                message="Harvest sweep aborted — kill switch is engaged.",
            )
            return jsonify({
                "message": "Kill switch active — harvest sweep skipped.",
                "kill_switch_active": True,
            }), 200
    except Exception as _ks_err:
        log.warning("harvest_sweep_kill_switch_read_failed", error=str(_ks_err),
                    fallback="Proceeding with harvest sweep despite kill-switch read failure.")

    # Pre-flight: ORCHESTRATOR_SA_EMAIL
    if not ORCHESTRATOR_SA_EMAIL:
        log.critical("harvest_sweep_sa_email_missing",
                     action="Set ORCHESTRATOR_SA_EMAIL env var in Cloud Run.")
        return jsonify({"error": "Precondition Failed", "code": "ORCHESTRATOR_SA_EMAIL_MISSING"}), 412

    base_url = PIPELINE_URL.split("/dispatch")[0].strip()
    if not base_url:
        log.critical("harvest_sweep_pipeline_url_missing",
                     action="Set PIPELINE_URL env var in Cloud Run.")
        return jsonify({"error": "Precondition Failed", "code": "PIPELINE_URL_MISSING"}), 412

    harvest_url = f"{base_url}/harvest"
    sa_email    = ORCHESTRATOR_SA_EMAIL
    now_utc     = datetime.datetime.now(datetime.timezone.utc)

    # Fetch all active campaigns
    campaigns = list(
        _db().collection("campaigns")
          .where(filter=FieldFilter("status", "==", "active"))
          .limit(500)
          .stream()
    )
    log.info("harvest_sweep_query_executed", campaign_count=len(campaigns))

    from core.clients import get_tasks_client as _get_tasks_client
    from google.protobuf import timestamp_pb2 as _ts_pb2
    tasks_client = _get_tasks_client()
    queue_path   = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)

    dispatched = skipped = 0

    for camp_doc in campaigns:
        campaign_data = camp_doc.to_dict() or {}
        campaign_id   = camp_doc.id
        tenant_id     = campaign_data.get("tenant_id")

        if not tenant_id:
            log.warning("harvest_sweep_no_tenant_id", campaign_id=campaign_id)
            skipped += 1
            continue

        # Credit gate — skip campaigns with no remaining credits
        try:
            user_data = _db().collection("users").document(tenant_id).get().to_dict() or {}
            wallet    = user_data.get("wallet", {}) or {}
            remaining = int(wallet.get("remaining_credits", 0) or 0)
            if remaining <= 0:
                log.info("harvest_sweep_credit_gate_skip",
                         tenant_id=tenant_id, campaign_id=campaign_id,
                         remaining_credits=remaining)
                skipped += 1
                continue
        except Exception as _cg_err:
            log.warning("harvest_sweep_credit_gate_failed",
                        tenant_id=tenant_id, error=str(_cg_err),
                        fallback="Proceeding with harvest task dispatch.")

        # Dispatch /harvest Cloud Task with jitter 0–60s
        jitter = _random.randint(0, 60)
        try:
            _when = now_utc + datetime.timedelta(seconds=jitter)
            _ts   = _ts_pb2.Timestamp()
            _ts.FromDatetime(_when)

            tasks_client.create_task(request={
                "parent": queue_path,
                "task": {
                    "schedule_time": _ts,
                    "http_request": {
                        "http_method": tv2.HttpMethod.POST,
                        "url":         harvest_url,
                        "headers":     {"Content-Type": "application/json"},
                        "body":        json.dumps({
                            "tenant_id":   tenant_id,
                            "campaign_id": campaign_id,
                        }).encode(),
                        "oidc_token": {
                            "service_account_email": sa_email,
                            "audience":              base_url,
                        },
                    },
                },
            })
            dispatched += 1
            log.info("harvest_task_dispatched",
                     campaign_id=campaign_id, tenant_id=tenant_id, jitter_s=jitter)
        except Exception as task_err:
            log.error("harvest_task_dispatch_failed",
                      campaign_id=campaign_id, tenant_id=tenant_id,
                      error=str(task_err), exc_info=True)
            skipped += 1

    log.info("harvest_sweep_complete", dispatched=dispatched, skipped=skipped,
             campaigns=len(campaigns))
    return jsonify({
        "message":    f"V25.2.0 Harvest Sweep: {dispatched} tasks dispatched, {skipped} skipped.",
        "dispatched": dispatched,
        "skipped":    skipped,
        "campaigns":  len(campaigns),
    }), 200


# =============================================================================
# POST /api/telemetry/conversion_feedback  (X-API-Key auth)
# =============================================================================
@bp.route("/api/telemetry/conversion_feedback", methods=["POST"])
def conversion_feedback():
    from google.cloud import secretmanager
    from google.cloud import firestore

    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return jsonify({"error": "Unauthorized", "message": "Missing X-API-Key header."}), 401

    sm_client = secretmanager.SecretManagerServiceClient()
    try:
        stored_key = sm_client.access_secret_version(
            request={"name": f"projects/{PROJECT_ID}/secrets/api_gateway_key/versions/latest"}
        ).payload.data.decode("UTF-8").strip()
    except Exception as e:
        return jsonify({"error": "Internal Error", "message": "Key validation unavailable."}), 500

    if api_key != stored_key:
        return jsonify({"error": "Forbidden", "message": "Invalid API key."}), 403

    data    = request.json or {}
    lead_id = data.get("lead_id")
    status  = data.get("status")
    if not lead_id or status not in ("converted", "rejected"):
        return jsonify({"error": "Bad Request", "message": "Requires lead_id and status: converted|rejected"}), 400

    lead_doc = _db().collection("leads").document(lead_id).get()
    if not lead_doc.exists:
        return jsonify({"error": "Not Found", "message": f"Lead {lead_id} not found."}), 404

    lead_dict       = lead_doc.to_dict()
    tenant_id       = lead_dict.get("tenant_id")
    tech_stack      = lead_dict.get("tech_stack_found", [])
    sourcing_vector = lead_dict.get("sourcing_vector", "B2B")
    hiring_intent   = lead_dict.get("hiring_intent_found", "No")
    delta           = 1 if status == "converted" else -1

    pref_updates: dict = {}
    if hiring_intent == "Yes":
        pref_updates["preferences_weights.hiring_intent"] = firestore.Increment(delta)
    for tech in tech_stack:
        pref_updates[f"preferences_weights.tech_{tech}"] = firestore.Increment(delta)
    if status == "rejected":
        import re
        words = list(set(re.findall(r"\b\w{4,}\b", (lead_dict.get("pain_point") or "").lower())))[:3]
        if words:
            pref_updates["dynamic_blocklist"] = firestore.ArrayUnion(words)
    if tenant_id and pref_updates:
        try:
            _db().collection("users").document(tenant_id).set(pref_updates, merge=True)
        except Exception as e:
            log.warning("reverse_rlhf_backprop_failed", error=str(e))

    try:
        _db().collection("system_telemetry").document("vector_weights").set(
            {sourcing_vector: firestore.Increment(delta)}, merge=True
        )
    except Exception as e:
        log.warning("vector_weights_update_failed", error=str(e))

    try:
        _db().collection("leads").document(lead_id).update(
            {"status": status, "updatedAt": firestore.SERVER_TIMESTAMP}
        )
    except Exception as e:
        log.warning("reverse_rlhf_lead_status_update_failed", error=str(e))

    return jsonify({"status": "ok", "delta": delta, "vector": sourcing_vector}), 200


# =============================================================================
# POST /api/internal/cron/reflection
# =============================================================================
@bp.route("/api/internal/cron/reflection", methods=["POST"])
def cron_reflection():
    from google.cloud import firestore

    ok, err = _verify_oidc(request)
    if not ok:
        return jsonify({"error": err}), 401 if "Missing" in err else 403

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff  = now_utc - datetime.timedelta(days=7)
    scrubbed: list = []

    for outcome_status in ("converted", "failed"):
        try:
            for doc in (
                _db().collection("leads")
                  .where(filter=FieldFilter("status",    "==", outcome_status))
                  .where(filter=FieldFilter("updatedAt", ">=", cutoff))
                  .limit(50).stream()
            ):
                d = doc.to_dict()
                scrubbed.append({
                    "outcome":         d.get("status"),
                    "score":           d.get("score"),
                    "sourcing_vector": d.get("sourcing_vector", "B2B"),
                    "confidence_tier": d.get("confidence_tier", "High"),
                    "hiring_intent":   d.get("hiring_intent_found", "No"),
                    "tech_stack":      d.get("tech_stack_found", []),
                    "company_size":    d.get("company_size_tier", "Unknown"),
                    "pain_theme":      (d.get("pain_point") or "")[:80],
                })
        except Exception as e:
            log.warning("reflection_sample_failed", status=outcome_status, error=str(e))

    if not scrubbed:
        return jsonify({"status": "no_data", "message": "Insufficient sample."}), 200

    current_weights = get_vector_weights()
    prompt = f"""You are a global outreach intelligence system performing a weekly strategic audit.
Analyze these {len(scrubbed)} anonymized lead outcomes. Return ONLY a JSON object with updated integer
weights for exactly these keys: "B2B", "B2C", "B2B2C", "D2C".
CURRENT WEIGHTS: {json.dumps(current_weights)}
LEAD OUTCOMES: {json.dumps(scrubbed)}"""

    SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "B2B": {"type": "INTEGER"},
            "B2C": {"type": "INTEGER"},
            "B2B2C": {"type": "INTEGER"},
            "D2C": {"type": "INTEGER"},
        },
        "required": ["B2B", "B2C", "B2B2C", "D2C"],
    }

    model    = GenerativeModel("gemini-2.5-flash")
    response = model.generate_content(
        prompt,
        generation_config=GenerationConfig(response_mime_type="application/json", response_schema=SCHEMA, temperature=0.1),
    )
    try:
        new_weights = json.loads(response.text)
        valid_keys  = {"B2B", "B2C", "B2B2C", "D2C"}
        new_weights = {k: int(v) for k, v in new_weights.items() if k in valid_keys}
    except Exception as e:
        return jsonify({"error": "Reflection LLM failed", "details": str(e)}), 500

    _db().collection("system_telemetry").document("vector_weights").set(new_weights)
    log.info("reflection_complete", new_weights=new_weights, sample_size=len(scrubbed))
    return jsonify({"status": "reflection_complete", "sample_size": len(scrubbed), "new_weights": new_weights}), 200


# =============================================================================
# POST /api/internal/cron/ontology-decay
# =============================================================================
@bp.route("/api/internal/cron/ontology-decay", methods=["POST"])
def cron_ontology_decay():
    from google.cloud import firestore

    ok, err = _verify_oidc(request)
    if not ok:
        return jsonify({"error": err}), 401 if "Missing" in err else 403

    updated = skipped = errored = 0
    decay_log: list = []
    try:
        for doc in _db().collection("ontology_map").stream():
            d      = doc.to_dict()
            weight = d.get("baseline_weight", 1.0)
            diff   = weight - 1.0
            if abs(diff) < 0.001:
                skipped += 1
                continue
            new_weight = round(weight - diff * 0.10, 6)
            try:
                _db().collection("ontology_map").document(doc.id).update({
                    "baseline_weight": new_weight,
                    "last_decayed":    firestore.SERVER_TIMESTAMP,
                })
                decay_log.append({"path": doc.id, "old": weight, "new": new_weight})
                updated += 1
            except Exception as we:
                log.warning("ontology_decay_write_failed", path=doc.id, error=str(we))
                errored += 1
    except Exception as scan_err:
        return jsonify({"error": "Ontology scan failed", "details": str(scan_err)}), 500

    log.info("ontology_decay_complete", updated=updated, skipped=skipped, errored=errored)
    return jsonify({
        "status": "decay_complete", "updated": updated,
        "skipped": skipped, "errors": errored, "decay_applied": decay_log,
    }), 200


# =============================================================================
# POST /api/internal/inbound-sentiment-run
# V23.5 — Inbound Sentiment Radar trigger
# Called by Cloud Scheduler every 6 hours (or manually for testing).
# Runs inbound_sentiment_job.run() in a daemon thread — returns 202 immediately.
# =============================================================================
@bp.route("/api/internal/inbound-sentiment-run", methods=["POST"])
def trigger_inbound_sentiment():
    """
    Trigger the Inbound Sentiment Radar job.

    Security: Protected by X-CloudTasks-QueueName or X-Internal-Secret header.
    The job runs asynchronously — this endpoint always returns 202 within ~5 ms.
    """
    # Verify caller is Cloud Scheduler / internal (reuse existing OIDC check pattern)
    import os as _os
    internal_secret = _os.environ.get("INTERNAL_CRON_SECRET", "")
    provided_secret = request.headers.get("X-Internal-Secret", "")
    cloud_tasks_hdr = request.headers.get("X-CloudTasks-QueueName", "")

    # V24.2 (L9-2): If INTERNAL_CRON_SECRET is not configured, ALL requests are
    # rejected to prevent unauthenticated access. Require explicit opt-out by
    # setting INTERNAL_CRON_SECRET to a non-empty value.
    if not internal_secret:
        log.error(
            "inbound_sentiment_trigger_secret_not_configured",
            note="INTERNAL_CRON_SECRET env var is not set. All requests rejected. "
                 "Set this env var in Cloud Run to enable the inbound radar trigger.",
        )
        return jsonify({"error": "Service not configured — contact administrator"}), 503
    if provided_secret != internal_secret and not cloud_tasks_hdr:
        log.warning("inbound_sentiment_trigger_unauthorized",
                    has_queue_header=bool(cloud_tasks_hdr),
                    has_secret=bool(provided_secret))
        return jsonify({"error": "unauthorized"}), 401

    import threading

    def _run():
        try:
            # Late import — avoids import-time Firestore/Vertex SDK init
            from jobs.inbound_sentiment_job import run  # type: ignore[import]
            result = run()
            log.info("inbound_sentiment_job_finished", **result)
        except Exception as exc:
            log.error("inbound_sentiment_job_error", error=str(exc))

    t = threading.Thread(target=_run, daemon=True, name="inbound-sentiment-job")
    t.start()
    log.info("inbound_sentiment_trigger_accepted")
    return jsonify({"status": "accepted", "message": "Inbound sentiment job started"}), 202
