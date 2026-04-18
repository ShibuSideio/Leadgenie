"""
Pipeline-Main V23 — /dispatch + /finalize Blueprint (FULL IMPLEMENTATION).

THE CONSUMER — 4-Hour Drip Processor.
======================================
Pops exactly 10 URLs from campaigns/{id}.unprocessed_queue (destructive read).
Runs the PRISM engine (mode-routing → scrape → Gemini gate → DM generation).
Triggers credit settlement via Cloud Task to orchestrator.
Does NOT call Serper. If queue is empty, exits gracefully.

Auth: @require_tasks_oidc on all routes (Zero-Trust, V23 Amendment 1).

V23 Extraction (2026-04-18):
- Fully extracted from main.py monolith (lines 2397-2931).
- All db calls via _db() lazy accessor (V23 gRPC DCL safety).
- All vertex calls via init_vertex() lazy accessor.
- Structured TRACE-1..TRACE-10 logs replacing print() statements.
- _settle_credit() re-wired to use get_tasks_client() DCL accessor.
"""
from __future__ import annotations

import concurrent.futures as _cf
import datetime
import hashlib
import json
import os
import random
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.transaction import transactional as _firestore_transactional

from core.config import (  # type: ignore[import]
    PROJECT_ID, LOCATION, QUEUE, ORCHESTRATOR_URL, SCRAPER_HEAVY_URL,
    SERPER_API_KEY_NAME, VELOCITY_THRESHOLD,
)
from core.clients import get_db, get_tasks_client, get_sm_client  # type: ignore[import]
from core.logging import get_logger                                 # type: ignore[import]
from middleware.oidc import require_tasks_oidc                      # type: ignore[import]
from services.serper_service import (  # type: ignore[import]
    extract_root_domain, SOCIAL_DOMAINS, deep_context_serper_dork,
)
from services.gemini_service import pre_filter_gemini, final_score_and_dm  # type: ignore[import]

bp  = Blueprint("dispatch", __name__)
log = get_logger("pipeline.dispatch")


# ---------------------------------------------------------------------------
# Helpers — lazy accessors (V23 gRPC DCL safety)
# ---------------------------------------------------------------------------

def _db():
    return get_db()


def _get_secret(secret_name: str) -> str:
    """Resolve a Secret Manager secret string. Lazy — never at import time."""
    client   = get_sm_client()
    response = client.access_secret_version(request={"name": secret_name})
    return response.payload.data.decode("utf-8")


# ---------------------------------------------------------------------------
# FIX 2: Atomic Exclusivity Lock Acquisition (transactional)
# ---------------------------------------------------------------------------
@_firestore_transactional
def _acquire_lead_lock(transaction, lock_ref, now_utc):
    """
    Atomically acquires a global exclusivity lock.
    Returns True  → lock acquired (new or expired).
    Returns False → domain within 14-day exclusivity window; caller skips.
    Raises        → Firestore contention; caller skips.
    """
    snap = lock_ref.get(transaction=transaction)
    if snap.exists:
        locked_until = snap.to_dict().get("locked_until")
        if locked_until:
            if hasattr(locked_until, "tzinfo") and locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=datetime.timezone.utc)
            if locked_until > now_utc:
                return False
    transaction.set(lock_ref, {"locked_until": now_utc + datetime.timedelta(days=14)})
    return True


# ---------------------------------------------------------------------------
# Credit settlement — async Cloud Task to orchestrator
# ---------------------------------------------------------------------------

def _settle_credit(tenant_id: str, outcome: str, count: int = 1, lead_id: str = ""):
    """
    Non-blocking credit settlement via Cloud Task.
    lead_id is the idempotency key — the orchestrator atomically stamps
    credit_settled=True before applying the wallet Increment.
    Falls back to direct shard write if ORCHESTRATOR_URL is unset.
    """
    if not ORCHESTRATOR_URL:
        try:
            shard_id = random.randint(0, 9)
            if outcome == "success":
                _db().collection("users").document(tenant_id) \
                    .collection("wallet_shards").document(str(shard_id)) \
                    .set({"consumed_credits": firestore.Increment(1)}, merge=True)
        except Exception as fb_e:
            log.warning("settle_credit_fallback_failed", tenant_id=tenant_id, error=str(fb_e))
        return

    try:
        from google.cloud import tasks_v2 as _tv2  # static import — no dynamic __import__
        tc         = get_tasks_client()
        queue_path = tc.queue_path(PROJECT_ID, LOCATION, QUEUE)
        body       = json.dumps({
            "tenant_id": tenant_id,
            "outcome":   outcome,
            "count":     count,
            "lead_id":   lead_id,
        }).encode()
        tc.create_task(parent=queue_path, task={
            "http_request": {
                "http_method": _tv2.HttpMethod.POST,
                "url":         f"{ORCHESTRATOR_URL}/api/internal/credits/settle",
                "headers":     {"Content-Type": "application/json"},
                "body":        body,
            }
        })
        log.info("settle_credit_enqueued", tenant_id=tenant_id,
                 outcome=outcome, lead_id=(lead_id[:12] if lead_id else "N/A"))
    except Exception as e:
        log.warning("settle_credit_enqueue_failed", tenant_id=tenant_id,
                    error=str(e), note="Non-fatal — pipeline continues.")


# ---------------------------------------------------------------------------
# /dispatch — Consumer Route
# ---------------------------------------------------------------------------

@bp.route("/dispatch", methods=["POST"])
@require_tasks_oidc
def dispatch():
    """
    V23 Consumer — PRISM engine execution.

    TRACE log convention:
        ``jsonPayload.message =~ "TRACE-[0-9]+"``
    """
    queue_name = request.headers.get("X-CloudTasks-QueueName", "MISSING")
    lead_data  = request.json or {}

    campaign_id = lead_data.get("campaign_id") or (
        (lead_data.get("matched_campaigns") or [None])[0]
    )
    tenant_id = lead_data.get("tenant_id")

    log.info("TRACE-1: dispatch() entered.",
             queue=queue_name, campaign_id=campaign_id, tenant_id=tenant_id)

    if not campaign_id:
        log.error("dispatch_no_campaign_id", payload=str(lead_data)[:200])
        return jsonify({"error": "Missing campaign_id context"}), 400

    # ── TRACE-2: Fetch campaign document ────────────────────────────────────
    log.info("TRACE-2: Fetching campaign document from Firestore.",
             campaign_id=campaign_id)
    campaign_ref = _db().collection("campaigns").document(campaign_id)
    try:
        campaign = campaign_ref.get().to_dict() or {}
    except Exception as cget_err:
        log.error("dispatch_campaign_fetch_failed", campaign_id=campaign_id,
                  error=str(cget_err), exc_info=True)
        return jsonify({"error": "Firestore timeout fetching campaign"}), 500

    bio             = campaign.get("bio", "")
    sourcing_vector = campaign.get("sourcing_vector", "Classic B2B")
    location        = campaign.get("location", "").strip()

    # ── PERSONA VAULT: inject persona bio (V23 precedence) ──────────────────
    persona_id = campaign.get("persona_id", "")
    if persona_id:
        persona_bio = campaign.get("persona_bio", "").strip()
        if persona_bio:
            bio = persona_bio
            log.info("dispatch_persona_injected",
                     persona_name=campaign.get("persona_name", persona_id),
                     campaign_id=campaign_id)

    log.info("TRACE-3: Campaign loaded.",
             campaign_id=campaign_id,
             sourcing_vector=sourcing_vector,
             queue_depth=len(campaign.get("unprocessed_queue", [])))

    # ── TRACE-4: Fetch active campaigns for tenant swarm context ────────────
    log.info("TRACE-4: Fetching active campaign swarm for tenant.", tenant_id=tenant_id)
    try:
        active_campaigns = []
        for doc in (_db().collection("campaigns")
                    .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                    .where(filter=FieldFilter("status", "==", "active"))
                    .stream()):
            d = doc.to_dict()
            d["id"] = doc.id
            active_campaigns.append(d)
    except Exception as ac_err:
        log.warning("dispatch_active_campaigns_failed", error=str(ac_err),
                    note="Using current campaign only.")
        active_campaigns = []
    if not active_campaigns:
        active_campaigns = [campaign]

    # ── Target personas — load from tenant_profiles if not on campaign doc ──
    raw_personas = campaign.get("target_personas", [])
    if not raw_personas:
        try:
            profile_snap = _db().collection("tenant_profiles").document(tenant_id).get()
            if profile_snap.exists:
                raw_personas = profile_snap.to_dict().get("target_personas", [])
                log.info("dispatch_personas_loaded_from_profile",
                         count=len(raw_personas), tenant_id=tenant_id)
        except Exception as pe:
            log.warning("dispatch_persona_profile_failed", error=str(pe))
    if not raw_personas and bio:
        raw_personas = [{"name": "Target Persona", "description": bio,
                         "location_hint": location or "Global"}]
        log.info("dispatch_persona_bio_fallback", campaign_id=campaign_id)
    campaign["target_personas"] = raw_personas

    # ── TRACE-5: Instantiate PrismPipeline ──────────────────────────────────
    log.info("TRACE-5: Instantiating PrismPipeline.", campaign_id=campaign_id,
             persona_count=len(raw_personas))
    prism = None
    try:
        # Import from monolith module — PrismPipeline is not yet extracted.
        # V23 constraint: only import classes, never use module-scope db/app objects.
        import importlib.util, sys as _sys
        _pm_path = os.path.join(os.path.dirname(__file__), "..", "..", "main.py")
        _pm_path = os.path.normpath(_pm_path)
        if "_pm_monolith_prism" not in _sys.modules:
            _spec = importlib.util.spec_from_file_location("_pm_monolith_prism", _pm_path)
            _mod  = importlib.util.module_from_spec(_spec)          # type: ignore[arg-type]
            # Stub out flask app/db to prevent module-scope side-effects
            import types
            _mod.app = types.SimpleNamespace(route=lambda *a, **kw: (lambda f: f))
            _mod.db  = _db()
            _sys.modules["_pm_monolith_prism"] = _mod
            try:
                _spec.loader.exec_module(_mod)                      # type: ignore[union-attr]
            except Exception:
                pass
        _mod           = _sys.modules["_pm_monolith_prism"]
        PrismPipeline  = _mod.PrismPipeline
        _serper_key    = _get_secret(SERPER_API_KEY_NAME).strip()
        prism          = PrismPipeline(campaign, _db(), _serper_key)
        log.info("dispatch_prism_instantiated", campaign_id=campaign_id,
                 persona_count=len(raw_personas))
    except Exception as prism_err:
        log.warning("dispatch_prism_init_failed", error=str(prism_err),
                    note="Falling back to empty-text path (scraper-heavy deferrals).")

    # ── TRACE-6: Destructive Queue Pop (Batch of 10) ────────────────────────
    current_queue = campaign.get("unprocessed_queue", [])
    if not current_queue:
        log.info("TRACE-6: unprocessed_queue empty — exiting gracefully.",
                 campaign_id=campaign_id)
        return jsonify({"status": "queue_empty", "processed": 0}), 200

    BATCH_SIZE = 10
    batch_urls = current_queue[:BATCH_SIZE]
    remaining  = current_queue[BATCH_SIZE:]
    campaign_ref.update({"unprocessed_queue": remaining})
    log.info("TRACE-6: Destructive queue pop complete.",
             campaign_id=campaign_id, batch_size=len(batch_urls),
             remaining=len(remaining))

    # ── User preferences (RLHF weights) ─────────────────────────────────────
    try:
        user_doc            = _db().collection("users").document(tenant_id).get()
        preferences_weights = (user_doc.to_dict() or {}).get("preferences_weights", {})
    except Exception as udoc_err:
        log.warning("dispatch_user_doc_failed", error=str(udoc_err))
        preferences_weights = {}

    # ── Hydrate snippet_map from scraped_cache (Producer hand-off) ──────────
    snippet_map = {}
    SOCIAL_SET  = set(SOCIAL_DOMAINS)
    for batch_url in batch_urls:
        b_domain  = extract_root_domain(batch_url)
        is_social = any(b_domain.endswith(s) for s in SOCIAL_SET)
        if is_social:
            try:
                cache_key = batch_url.replace("/", "_")
                cdoc = _db().collection("scraped_cache").document(cache_key).get()
                if cdoc.exists:
                    txt = cdoc.to_dict().get("text", "")
                    if txt:
                        snippet_map[batch_url] = txt
            except Exception:
                pass

    # ── TRACE-7: Confidence Tiering Gate (pre_filter_gemini) ────────────────
    log.info("TRACE-7: Calling pre_filter_gemini.", url_count=len(batch_urls))
    synthetic_snippets = [
        {"link": u, "snippet": snippet_map.get(u, ""), "title": ""}
        for u in batch_urls
    ]
    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as pool:
            fut    = pool.submit(pre_filter_gemini, synthetic_snippets, bio, location)
            tiered = fut.result(timeout=30)
    except Exception as gate_err:
        log.warning("dispatch_pre_filter_timeout", error=str(gate_err),
                    note="Treating all URLs as High-tier.")
        tiered = {"High": batch_urls, "Medium": [], "Low": []}

    high_urls   = tiered.get("High", [])
    medium_urls = tiered.get("Medium", [])

    # Velocity gate (Medium URLs)
    velocity_threshold = VELOCITY_THRESHOLD
    try:
        cutoff_24h   = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        recent_count = (
            _db().collection("leads")
            .where("tenant_id", "==", tenant_id)
            .where("status",    "==", "new")
            .where("createdAt", ">=", cutoff_24h)
            .count().get()[0][0].value
        )
    except Exception:
        recent_count = 0

    allow_medium  = recent_count < velocity_threshold
    approved_urls = high_urls + (medium_urls if allow_medium else [])
    url_to_tier   = {u: "High" for u in high_urls}
    url_to_tier.update({u: "Medium" for u in medium_urls})
    log.info("TRACE-7: Gate complete.",
             high=len(high_urls), medium=len(medium_urls),
             approved=len(approved_urls), gate_rejected=len(batch_urls)-len(approved_urls))

    # ── TRACE-8: PRISM Processing Loop ──────────────────────────────────────
    log.info("TRACE-8: Entering PRISM processing loop.", url_count=len(approved_urls))
    all_results    = []
    scrape_success = scrape_failed = 0

    for url in approved_urls:
        target_domain = extract_root_domain(url)
        if not target_domain:
            continue

        is_social = any(target_domain.endswith(s) for s in SOCIAL_SET)
        if is_social:
            parsed     = urlparse(url)
            exact_path = f"{parsed.netloc}{parsed.path}".lower().replace("www.", "")
            lock_entity   = hashlib.sha256(exact_path.encode()).hexdigest()
            dedupe_target = exact_path
        else:
            lock_entity   = target_domain
            dedupe_target = target_domain

        # ── Global Exclusivity Lock (atomic transactional) ───────────────────
        lock_ref = _db().collection("global_lead_locks").document(lock_entity)
        try:
            now_utc   = datetime.datetime.now(datetime.timezone.utc)
            lock_txn  = _db().transaction()
            acquired  = _acquire_lead_lock(lock_txn, lock_ref, now_utc)
            if not acquired:
                log.info("dispatch_exclusivity_skip", url=url[:80],
                         lock_entity=lock_entity, note="14-day window active.")
                continue
        except Exception as lock_err:
            log.warning("dispatch_lock_failed", url=url[:80], error=str(lock_err),
                        note="Skipping URL to prevent duplicate lead.")
            continue

        # ── Deterministic Dedup Gateway ──────────────────────────────────────
        lead_id_str = f"{tenant_id}_{dedupe_target}"
        lead_id     = hashlib.sha256(lead_id_str.encode()).hexdigest()
        doc_ref     = _db().collection("leads").document(lead_id)

        try:
            expire_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=90)
            doc_ref.create({
                "tenant_id":         tenant_id,
                "matched_campaigns": [campaign_id],
                "url":               url,
                "lock_entity":       lock_entity,
                "confidence_tier":   url_to_tier.get(url, "High"),
                "sourcing_vector":   sourcing_vector,
                "status":            "processing",
                "is_in_crm":         False,
                "createdAt":         firestore.SERVER_TIMESTAMP,
                "expire_at":         expire_at,
            })
        except Exception as already_err:
            if "already exists" in str(already_err).lower():
                log.info("dispatch_cross_campaign_dup", domain=target_domain)
                doc_ref.update({"matched_campaigns": firestore.ArrayUnion([campaign_id])})
            else:
                log.warning("dispatch_stub_create_failed", url=url[:80],
                            error=str(already_err))
            continue

        try:
            # ── PRISM ENGINE ─────────────────────────────────────────────────
            text = tech_stack = emails = phones = ""
            tech_stack, emails, phones = [], [], []
            prism_mode    = "legacy"
            fallback_used = False

            if prism is not None:
                hook         = prism.process_url(url, tenant_id)
                text         = hook.get("text", "")
                tech_stack   = hook.get("tech_stack", [])
                emails       = hook.get("emails", [])
                phones       = hook.get("phones", [])
                prism_mode   = hook.get("mode", "GeneralDomain")
                fallback_used = hook.get("fallback_used", False)

                if not text:
                    # Defer to scraper-heavy via Cloud Task
                    log.info("dispatch_prism_empty_text_defer", url=url[:80],
                             mode=prism_mode)
                    _defer_to_scraper_heavy(url, lead_id, tenant_id, campaign_id,
                                            bio, target_domain, preferences_weights)
                    continue
            else:
                # Prism init failed — defer all URLs to scraper-heavy
                _defer_to_scraper_heavy(url, lead_id, tenant_id, campaign_id,
                                        bio, target_domain, preferences_weights)
                continue

            # Stamp prism_mode on stub
            try:
                doc_ref.update({"prism_mode": prism_mode, "fallback_used": fallback_used})
            except Exception:
                pass

            if not text:
                log.warning("dispatch_dead_payload", url=url[:80])
                doc_ref.update({"status": "failed_scrape",
                                "error": "Empty DOM — no cache, no snippet, no scrape"})
                scrape_failed += 1
                continue

            # WAF/bot detection
            bot_keywords = ["Cloudflare Ray ID", "Please verify you are human",
                            "Enable JavaScript and cookies to continue",
                            "Access Denied", "403 Forbidden"]
            if any(kw.lower() in text.lower() for kw in bot_keywords):
                doc_ref.update({"status": "failed", "error": "Blocked by Cloudflare/WAF"})
                scrape_failed += 1
                continue

            # Usage metrics
            shard_id = random.randint(0, 9)
            _db().collection("usage_metrics").document(tenant_id) \
                .collection("shards").document(str(shard_id)) \
                .set({"gemini_calls": firestore.Increment(1)}, merge=True)

            # Deep context Serper dork
            context_payload, native_hiring_intent = deep_context_serper_dork(
                target_domain, tenant_id, sourcing_vector, source_url=url
            )

            # RLHF fit score
            fit_score = 0
            if native_hiring_intent:
                fit_score += preferences_weights.get("hiring_intent", 0)
            for tech in tech_stack:
                fit_score += preferences_weights.get(f"tech_{tech}", 0)
            if fit_score <= -3:
                log.info("dispatch_rlhf_drop", domain=target_domain, fit_score=fit_score)
                doc_ref.delete()
                try:
                    _db().collection("global_lead_locks").document(lock_entity).delete()
                except Exception:
                    pass
                continue

            # ── TRACE-9: final_score_and_dm — Gemini scoring ─────────────────
            log.info("TRACE-9: Calling final_score_and_dm.", url=url[:80])
            try:
                evaluation = final_score_and_dm(
                    text, active_campaigns, context_payload, tech_stack, source_url=url
                )
            except TimeoutError:
                doc_ref.update({"status": "failed", "error": "Vertex AI timeout"})
                scrape_failed += 1
                continue
            except Exception as eval_err:
                doc_ref.update({"status": "failed", "error": str(eval_err)})
                scrape_failed += 1
                continue

            # ── Dynamic acceptance threshold ──────────────────────────────────
            is_shadow_thin   = text.strip().startswith("[SHADOW_LEARNER_THIN_PAYLOAD]")
            is_thin_payload  = is_shadow_thin or len(text.strip()) < 500
            accept_threshold = 6 if is_thin_payload else 7
            score            = evaluation.get("score", 0)

            if score >= accept_threshold:
                # Merge extracted emails/phones into contact_endpoints
                contact_endpoints = list(evaluation.get("contact_endpoints", []))
                existing_uris     = {e["uri"] for e in contact_endpoints}
                for em in (emails or [])[:3]:
                    if em and em not in existing_uris:
                        contact_endpoints.append({"platform": "email", "uri": em})
                        existing_uris.add(em)
                for ph in (phones or [])[:2]:
                    if ph and ph not in existing_uris:
                        contact_endpoints.append({"platform": "other", "uri": ph})
                        existing_uris.add(ph)

                # ── TRACE-10: Lead Firestore write ───────────────────────────
                log.info("TRACE-10: Writing qualified lead to Firestore.",
                         url=url[:80], score=score, campaign_id=campaign_id)
                lead_payload = {
                    "id":                           lead_id,
                    "source_url":                   url,
                    "tenant_id":                    tenant_id,
                    "origin_engine":                "cartographer",
                    "score":                        score,
                    "matched_campaign_ids":         evaluation.get("matched_campaign_ids", []),
                    "matched_campaigns":            [campaign_id],
                    "campaign_id":                  campaign_id,
                    "trend_mapped":                 evaluation.get("trend_mapped", False),
                    "highest_campaign_id":          evaluation.get("highest_campaign_id", "Unknown"),
                    "pain_point":                   evaluation.get("pain_point", ""),
                    "dm":                           evaluation.get("dm", ""),
                    "intent_signal":                evaluation.get("intent_signal", ""),
                    "hiring_intent_found":          evaluation.get("hiring_intent_found", "No"),
                    "tech_stack_found":             evaluation.get("tech_stack_found", []),
                    "icebreaker_angle":             evaluation.get("icebreaker_angle", ""),
                    "contact_endpoints":            contact_endpoints,
                    "decision_maker_name":          evaluation.get("decision_maker_name", "Unknown"),
                    "decision_maker_title":         evaluation.get("decision_maker_title", "Unknown"),
                    "company_size_tier":            evaluation.get("company_size_tier", "Unknown"),
                    "primary_objection_hypothesis": evaluation.get("primary_objection_hypothesis", "Unknown"),
                    "company_name":                 evaluation.get("company_name"),
                    "dossier_text":                 None,
                    "sourcing_vector":              sourcing_vector,
                    "confidence_tier":              url_to_tier.get(url, "High"),
                    "prism_mode":                   prism_mode,
                    "prism_fallback":               fallback_used,
                    "status":                       "new",
                    "is_in_crm":                    False,
                }
                doc_ref.set(lead_payload, merge=True)
                _settle_credit(tenant_id, "success", lead_id=lead_id)
                scrape_success += 1
                all_results.append({"url": url, "score": score})

                # WhatsApp Business API notification (score >= 8)
                if score >= 8:
                    _maybe_notify_whatsapp(tenant_id, url, lead_id, score, evaluation)
            else:
                log.info("dispatch_score_gate_drop", url=url[:80],
                         score=score, threshold=accept_threshold)
                doc_ref.delete()
                try:
                    _db().collection("global_lead_locks").document(lock_entity).delete()
                except Exception:
                    pass

        except Exception as loop_err:
            log.error("dispatch_loop_crash", url=url[:80], campaign_id=campaign_id,
                      error=str(loop_err), exc_info=True)
            try:
                doc_ref.update({"status": "failed", "error": "Consumer pipeline crash"})
            except Exception:
                pass
            scrape_failed += 1
            continue

    log.info("dispatch_batch_complete", campaign_id=campaign_id,
             processed=len(all_results), scrape_success=scrape_success,
             scrape_failed=scrape_failed)
    return jsonify({
        "processed_leads": len(all_results),
        "scrape_success":  scrape_success,
        "scrape_failed":   scrape_failed,
    }), 200


# ---------------------------------------------------------------------------
# /finalize — scraper-heavy async completion webhook
# ---------------------------------------------------------------------------

@bp.route("/finalize", methods=["POST"])
@require_tasks_oidc
def finalize():
    """Receives completed scrape result from scraper-heavy Cloud Task."""
    data      = request.json or {}
    lead_id   = data.get("lead_id")
    tenant_id = data.get("tenant_id")

    log.info("TRACE-1: finalize() entered.",
             lead_id=(lead_id or "MISSING")[:24], tenant_id=tenant_id)

    if not lead_id or not tenant_id:
        return jsonify({"error": "Missing lead_id or tenant_id"}), 400

    text         = data.get("text", "")
    emails       = data.get("emails", [])
    phones       = data.get("phones", [])
    campaign_id  = data.get("campaign_id", "")
    bio          = data.get("bio", "")
    tech_stack   = data.get("tech_stack", [])

    doc_ref = _db().collection("leads").document(lead_id)
    snap    = doc_ref.get()
    if not snap.exists:
        return jsonify({"error": "Lead stub not found"}), 404

    lead_data       = snap.to_dict() or {}
    sourcing_vector = lead_data.get("sourcing_vector", "Classic B2B")
    active_campaigns = [lead_data]  # minimal context for scoring

    if not text:
        doc_ref.update({"status": "failed_scrape",
                        "error": "scraper-heavy returned empty text"})
        return jsonify({"status": "failed_scrape"}), 200

    context_payload, _ = deep_context_serper_dork(
        lead_data.get("url", ""), tenant_id, sourcing_vector
    )

    try:
        evaluation = final_score_and_dm(
            text, active_campaigns, context_payload, tech_stack,
            source_url=lead_data.get("url", "")
        )
    except Exception as eval_err:
        doc_ref.update({"status": "failed", "error": str(eval_err)})
        return jsonify({"error": str(eval_err)}), 500

    score = evaluation.get("score", 0)
    is_thin = len(text.strip()) < 500
    threshold = 6 if is_thin else 7

    if score >= threshold:
        contact_endpoints = list(evaluation.get("contact_endpoints", []))
        existing_uris     = {e["uri"] for e in contact_endpoints}
        for em in (emails or [])[:3]:
            if em and em not in existing_uris:
                contact_endpoints.append({"platform": "email", "uri": em})
        for ph in (phones or [])[:2]:
            if ph and ph not in existing_uris:
                contact_endpoints.append({"platform": "other", "uri": ph})

        doc_ref.set({
            "score":       score,
            "dm":          evaluation.get("dm", ""),
            "pain_point":  evaluation.get("pain_point", ""),
            "contact_endpoints": contact_endpoints,
            "status":      "new",
        }, merge=True)
        _settle_credit(tenant_id, "success", lead_id=lead_id)
        log.info("finalize_lead_written", lead_id=lead_id[:24], score=score)
    else:
        doc_ref.delete()
        log.info("finalize_score_gate_drop", lead_id=lead_id[:24], score=score)

    return jsonify({"status": "ok", "score": score}), 200


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _defer_to_scraper_heavy(url: str, lead_id: str, tenant_id: str,
                             campaign_id: str, bio: str, target_domain: str,
                             preferences_weights: dict) -> None:
    """Enqueue a Cloud Task to scraper-heavy for JS-heavy / WAF-blocked URLs."""
    try:
        from google.cloud import tasks_v2 as _tv2
        tc      = get_tasks_client()
        parent  = tc.queue_path(PROJECT_ID, LOCATION, QUEUE)
        body    = json.dumps({
            "url": url, "lead_id": lead_id, "tenant_id": tenant_id,
            "campaign_id": campaign_id, "bio": bio,
            "target_domain": target_domain,
            "preferences_weights": preferences_weights,
        }).encode()
        tc.create_task(parent=parent, task={
            "http_request": {
                "http_method": _tv2.HttpMethod.POST,
                "url":         SCRAPER_HEAVY_URL,
                "headers":     {"Content-Type": "application/json"},
                "body":        body,
            }
        })
        log.info("dispatch_deferred_to_scraper_heavy", url=url[:80])
    except Exception as defer_err:
        log.warning("dispatch_scraper_heavy_defer_failed",
                    url=url[:80], error=str(defer_err))


def _maybe_notify_whatsapp(tenant_id: str, url: str, lead_id: str,
                            score: int, evaluation: dict) -> None:
    """Fire WhatsApp Business API notification for score >= 8 leads."""
    try:
        import httpx as _httpx
        tenant_doc         = _db().collection("users").document(tenant_id).get().to_dict() or {}
        wa_token_encrypted = tenant_doc.get("wa_token")
        wa_phone_id        = tenant_doc.get("wa_phone_id")
        admin_phone        = tenant_doc.get("admin_phone")
        if not (wa_token_encrypted and wa_phone_id and admin_phone):
            return
        try:
            from core.config import get_cipher  # type: ignore[import]
            wa_token = get_cipher().decrypt(wa_token_encrypted.encode()).decode()
        except Exception:
            wa_token = wa_token_encrypted
        _httpx.post(
            f"https://graph.facebook.com/v18.0/{wa_phone_id}/messages",
            json={
                "messaging_product": "whatsapp",
                "to": admin_phone, "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": (
                        f"\U0001f525 Hot Lead!\n{url}\n"
                        f"Score: {score}/10\n{evaluation.get('pain_point', '')}\n\n"
                        f"DM: {evaluation.get('dm', '')}"
                    )},
                    "action": {"buttons": [
                        {"type": "reply", "reply": {"id": f"approve_{lead_id}", "title": "\u2705 Approve"}},
                        {"type": "reply", "reply": {"id": f"ignore_{lead_id}",  "title": "\U0001f6ab Ignore"}},
                    ]},
                },
            },
            headers={"Authorization": f"Bearer {wa_token}",
                     "Content-Type": "application/json"},
            timeout=5,
        )
        log.info("whatsapp_notification_sent", tenant_id=tenant_id, score=score)
    except Exception as wa_err:
        log.warning("whatsapp_notification_failed", error=str(wa_err))
