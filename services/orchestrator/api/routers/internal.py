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
except ImportError:
    pass  # Handled at startup

import random

db = get_db()

bp = Blueprint("internal", __name__)
log = get_logger("orchestrator.v23.internal")


def _verify_oidc(request_obj) -> tuple[bool, str]:
    """Return (is_valid, error_detail)."""
    auth = request_obj.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False, "Missing OIDC token"
    token = auth.split("Bearer ")[1]
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        id_token.verify_oauth2_token(token, google_requests.Request())
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
    user_ref = db.collection("users").document(tenant_id)
    lead_ref = db.collection("leads").document(lead_id) if lead_id else None
    try:
        txn = db.transaction()
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

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    # ── Circuit Breaker ──────────────────────────────────────────────────────
    SERPER_429_THRESHOLD  = float(__import__("os").environ.get("CB_SERPER_THRESHOLD",  "0.15"))
    SCRAPER_OOM_THRESHOLD = float(__import__("os").environ.get("CB_SCRAPER_THRESHOLD", "0.05"))
    CB_WINDOW_MINUTES     = int(__import__("os").environ.get("CB_WINDOW_MINUTES", "15"))

    try:
        cb_data = (db.collection("system_telemetry").document("circuit_breaker_state").get().to_dict() or {})
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
            reason = f"serper={serper_rate*100:.1f}% | scraper={scraper_rate*100:.1f}%"
            log.warning("circuit_breaker_open", reason=reason)
            return jsonify({"circuit_breaker": "open", "reason": reason}), 503
    except Exception as cb_err:
        log.warning("circuit_breaker_read_failed_fail_open", error=str(cb_err))

    # ── Main sweep ───────────────────────────────────────────────────────────
    campaigns = list(
        db.collection("campaigns")
          .where(filter=FieldFilter("status", "==", "active"))
          .limit(500)
          .stream()
    )
    # DIAGNOSTIC: always visible in Cloud Logging — confirms query executed
    log.info("sweep_query_executed",
             campaign_count=len(campaigns),
             note="Only status==active filter applied. No secondary filters.")

    audit_trail   = [f"Executed V23 Dual-Mode Sweep. Found {len(campaigns)} active campaigns."]
    tasks_client  = tv2.CloudTasksClient()
    queue_path    = tasks_client.queue_path(PROJECT_ID, LOCATION, QUEUE)

    # ── OIDC identity — static config first, 1-second metadata fallback ─────
    # PREVIOUS BUG: get_service_account_email() was called here, making 3
    # blocking urllib calls (up to 18s) to metadata.google.internal.
    # On cold starts or slow metadata this returned "" → no OIDC token →
    # Cloud Tasks hit pipeline-main with HTTP 403 from Cloud Run IAM.
    #
    # FIX: Read ORCHESTRATOR_SA_EMAIL env var set in Cloud Run console.
    # Only falls back to metadata if env var is absent, with a 1-second hard cap.
    if ORCHESTRATOR_SA_EMAIL:
        sa_email = ORCHESTRATOR_SA_EMAIL
        log.info("oidc_sa_email_from_config", sa_email=sa_email)
    else:
        try:
            import urllib.request as _ur
            import concurrent.futures as _cf
            _meta_url = (
                "http://metadata.google.internal/computeMetadata/v1"
                "/instance/service-accounts/default/email"
            )
            def _fetch_sa():
                req = _ur.Request(_meta_url, headers={"Metadata-Flavor": "Google"})
                with _ur.urlopen(req, timeout=1) as r:
                    return r.read().decode("utf-8").strip()
            with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                sa_email = _pool.submit(_fetch_sa).result(timeout=1)
            log.info("oidc_sa_email_from_metadata", sa_email=sa_email)
        except Exception as _meta_err:
            sa_email = ""
            log.warning("sa_email_metadata_fallback_failed",
                        error=str(_meta_err),
                        action="Tasks will be dispatched WITHOUT OIDC token. "
                               "Set ORCHESTRATOR_SA_EMAIL env var to permanently fix.")

    base_url    = PIPELINE_URL.split("/dispatch")[0]
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
        if sa_email:
            t["http_request"]["oidc_token"] = {
                "service_account_email": sa_email,
                "audience": base_url,
            }
        else:
            log.warning("oidc_token_missing_no_sa_email",
                        url=url,
                        note="Task dispatched without OIDC. Will be rejected with "
                             "403 by Cloud Run IAM. Set ORCHESTRATOR_SA_EMAIL.")
        return t

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

        # ── Quota gate ───────────────────────────────────────────────────────
        # NOTE: We do NOT check approval_status here. Campaigns can only exist
        # for tenants who were approved at creation time. Legacy documents
        # pre-dating the approval_status field would have approval_status=None
        # and would be silently blocked by check_quota's != 'approved' guard.
        # Instead: super_admin always passes; others are gated on credit balance.
        user_doc  = (db.collection("users").document(tenant_id).get().to_dict() or {})
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
            try:
                jitter  = random.randint(1, 120)
                sched_t = ts_pb2.Timestamp()
                sched_t.FromDatetime(now_utc + datetime.timedelta(seconds=jitter))
                task    = _oidc_task(produce_url, {"tenant_id": tenant_id, "campaign_id": campaign_id})
                task["schedule_time"] = sched_t
                tasks_client.create_task(request={"parent": queue_path, "task": task})
                produce_dispatched += 1
                log.info("produce_task_dispatched",
                         campaign_id=campaign_id, tenant_id=tenant_id, jitter_s=jitter)
                audit_trail.append(f"🏭 PRODUCER queued for {campaign_id} (jitter={jitter}s)")
            except Exception as prod_err:
                log.error("producer_dispatch_failed", campaign_id=campaign_id,
                          tenant_id=tenant_id, error=str(prod_err), exc_info=True)
                audit_trail.append(f"❌ PRODUCER ERROR {campaign_id}: {prod_err}")
            finally:
                # Clock MUST advance regardless of task dispatch success/failure.
                # datetime → ISO-8601 string: avoids Firestore protobuf serialization
                # rejection of offset-aware datetime objects on some SDK versions.
                try:
                    _next_produce = (now_utc + datetime.timedelta(hours=PRODUCE_INTERVAL_H)).isoformat()
                    camp_doc.reference.update({"next_produce_due": _next_produce})
                    log.info("produce_clock_advanced", campaign_id=campaign_id,
                             next_produce_due=_next_produce)
                except Exception as ts_err:
                    log.error("produce_timestamp_update_failed", campaign_id=campaign_id,
                              error=str(ts_err), exc_info=True)

        # ── Consumer (4h interval) ────────────────────────────────────────────
        DRIP_INTERVAL_H = 4
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
                else:
                    jitter  = random.randint(1, 290)
                    sched_t = ts_pb2.Timestamp()
                    sched_t.FromDatetime(now_utc + datetime.timedelta(seconds=jitter))
                    task    = _oidc_task(consume_url, {"tenant_id": tenant_id, "campaign_id": campaign_id})
                    task["schedule_time"] = sched_t
                    tasks_client.create_task(request={"parent": queue_path, "task": task})
                    consume_dispatched += 1
                    log.info("consume_task_dispatched",
                             campaign_id=campaign_id, tenant_id=tenant_id,
                             queue_depth=queue_depth, jitter_s=jitter)
                    audit_trail.append(f"⚙️ CONSUMER queued for {campaign_id} (depth={queue_depth})")

        except Exception as drip_err:
            log.error("consumer_dispatch_failed", campaign_id=campaign_id,
                      tenant_id=tenant_id, error=str(drip_err), exc_info=True)
            audit_trail.append(f"❌ CONSUMER ERROR {campaign_id}: {drip_err}")
        finally:
            # Clock MUST advance regardless of any exception above.
            # datetime → ISO-8601 string: Firestore-safe across all SDK versions.
            # This finally: is now bulletproof — nothing above can skip it.
            try:
                _next_drip = (now_utc + datetime.timedelta(hours=DRIP_INTERVAL_H)).isoformat()
                camp_doc.reference.update({
                    "next_drip_due":         _next_drip,
                    "drip_interval_minutes": DRIP_INTERVAL_H * 60,
                })
                log.info("drip_clock_advanced", campaign_id=campaign_id,
                         next_drip_due=_next_drip)
            except Exception as ts_err:
                log.error("drip_timestamp_update_failed", campaign_id=campaign_id,
                          error=str(ts_err), exc_info=True)


    # ── Zombie Lead Recovery ─────────────────────────────────────────────────
    from google.cloud import firestore as _fs
    ZOMBIE_MINS    = 15
    zombie_cutoff  = now_utc - datetime.timedelta(minutes=ZOMBIE_MINS)
    zombie_recovered = zombie_locks_released = 0
    try:
        zombie_docs = (
            db.collection("leads")
              .where(filter=FieldFilter("status",    "==", "processing"))
              .where(filter=FieldFilter("createdAt", "<",  zombie_cutoff))
              .limit(100).stream()
        )
        for zombie_doc in zombie_docs:
            zombie_data = zombie_doc.to_dict() or {}
            try:
                zombie_doc.reference.update({
                    "status":       "failed",
                    "error":        f"Zombie recovery >{ZOMBIE_MINS}min.",
                    "recovered_at": _fs.SERVER_TIMESTAMP,
                })
                zombie_recovered += 1
            except Exception:
                continue
            lock_entity = zombie_data.get("lock_entity")
            if lock_entity:
                try:
                    lr = db.collection("global_lead_locks").document(lock_entity)
                    if lr.get().exists:
                        lr.delete()
                        zombie_locks_released += 1
                except Exception:
                    pass
            zombie_tenant = zombie_data.get("tenant_id")
            if zombie_tenant:
                try:
                    db.collection("users").document(zombie_tenant).update(
                        {"wallet.reserved_credits": _fs.Increment(-1)}
                    )
                except Exception:
                    pass
    except Exception as zse:
        audit_trail.append(f"⚠️ Zombie sweep error: {zse}")

    log.info("cron_sweep_complete",
             producers=produce_dispatched, consumers=consume_dispatched,
             zombies=zombie_recovered)
    return jsonify({
        "message": f"V23 Sweep: {produce_dispatched} producers + {consume_dispatched} consumers.",
        "produce_dispatched":    produce_dispatched,
        "consume_dispatched":    consume_dispatched,
        "zombie_recovered":      zombie_recovered,
        "zombie_locks_released": zombie_locks_released,
        "audit_trail":           audit_trail,
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

    lead_doc = db.collection("leads").document(lead_id).get()
    if not lead_doc.exists:
        return jsonify({"error": "Not Found", "message": f"Lead {lead_id} not found."}), 404

    lead_dict       = lead_doc.to_dict()
    tenant_id       = lead_dict.get("tenant_id")
    tech_stack      = lead_dict.get("tech_stack_found", [])
    sourcing_vector = lead_dict.get("sourcing_vector", "Classic B2B")
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
            db.collection("users").document(tenant_id).set(pref_updates, merge=True)
        except Exception as e:
            log.warning("reverse_rlhf_backprop_failed", error=str(e))

    try:
        db.collection("system_telemetry").document("vector_weights").set(
            {sourcing_vector: firestore.Increment(delta)}, merge=True
        )
    except Exception as e:
        log.warning("vector_weights_update_failed", error=str(e))

    try:
        db.collection("leads").document(lead_id).update(
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
                db.collection("leads")
                  .where(filter=FieldFilter("status",    "==", outcome_status))
                  .where(filter=FieldFilter("updatedAt", ">=", cutoff))
                  .limit(50).stream()
            ):
                d = doc.to_dict()
                scrubbed.append({
                    "outcome":         d.get("status"),
                    "score":           d.get("score"),
                    "sourcing_vector": d.get("sourcing_vector", "Classic B2B"),
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
    prompt = f"""You are a global B2B outreach intelligence system performing a weekly strategic audit.
Analyze these {len(scrubbed)} anonymized lead outcomes. Return ONLY a JSON object with updated integer
weights for exactly these keys: "Classic B2B", "Social/Forum Listening", "Review Hijacking", "Maps/GMB Targeting".
CURRENT WEIGHTS: {json.dumps(current_weights)}
LEAD OUTCOMES: {json.dumps(scrubbed)}"""

    SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "Classic B2B": {"type": "INTEGER"},
            "Social/Forum Listening": {"type": "INTEGER"},
            "Review Hijacking": {"type": "INTEGER"},
            "Maps/GMB Targeting": {"type": "INTEGER"},
        },
        "required": ["Classic B2B", "Social/Forum Listening", "Review Hijacking", "Maps/GMB Targeting"],
    }

    model    = GenerativeModel("gemini-2.5-flash")
    response = model.generate_content(
        prompt,
        generation_config=GenerationConfig(response_mime_type="application/json", response_schema=SCHEMA, temperature=0.1),
    )
    try:
        new_weights = json.loads(response.text)
        valid_keys  = {"Classic B2B", "Social/Forum Listening", "Review Hijacking", "Maps/GMB Targeting"}
        new_weights = {k: int(v) for k, v in new_weights.items() if k in valid_keys}
    except Exception as e:
        return jsonify({"error": "Reflection LLM failed", "details": str(e)}), 500

    db.collection("system_telemetry").document("vector_weights").set(new_weights)
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
        for doc in db.collection("ontology_map").stream():
            d      = doc.to_dict()
            weight = d.get("baseline_weight", 1.0)
            diff   = weight - 1.0
            if abs(diff) < 0.001:
                skipped += 1
                continue
            new_weight = round(weight - diff * 0.10, 6)
            try:
                db.collection("ontology_map").document(doc.id).update({
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
