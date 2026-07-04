"""
Pipeline-Main V23 — /dispatch + /finalize Blueprint (FULL IMPLEMENTATION).

THE CONSUMER — 4-Hour Drip Processor.
======================================
Pops exactly 5 URLs from campaigns/{id}.unprocessed_queue (destructive read).
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

SF-013 FIX (2026-04-23):
- _shadow_track_bq() wired into _process_single_url() after a lead passes
  the score gate. Pushes one BQ row to swarm_analytics.shadow_track_events
  (lead quality signal) via a daemon thread — zero latency added to the
  scraping loop. Mirrors the produce-side BQ pattern for completeness.
"""
from __future__ import annotations

import concurrent.futures as _cf
import datetime
import hashlib
import json
import os
import random
import time
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.transaction import transactional as _firestore_transactional

from core.config import (  # type: ignore[import]
    PROJECT_ID, LOCATION, QUEUE, ORCHESTRATOR_URL, SCRAPER_HEAVY_URL,
    SERPER_API_KEY_NAME, VELOCITY_THRESHOLD, ORCHESTRATOR_SA_EMAIL,
)
from core.clients import get_db, get_tasks_client, get_sm_client, get_serper_key  # type: ignore[import]
from core.logging import get_logger                                 # type: ignore[import]
from middleware.oidc import require_tasks_oidc                      # type: ignore[import]
from services.serper_service import (  # type: ignore[import]
    extract_root_domain, SOCIAL_DOMAINS, deep_context_serper_dork,
)
from services.gemini_service import pre_filter_gemini, final_score_and_dm  # type: ignore[import]
from services.prism_pipeline import PrismPipeline                           # type: ignore[import]
from services.query_brain import _is_consumer_archetype  # type: ignore[import]
from services.intelligence_mesh import enrich_signals  # type: ignore[import]  # V24.0
# SF-002 FIX: PrismPipeline is now imported from the standalone
# services/prism_pipeline.py module (zero import-time side effects).
# The previous importlib.exec_module(main.py) approach executed 3185 lines
# of monolith code including Flask app creation and Fernet(ENCRYPTION_KEY)
# which crashed the worker on env var gaps. This import is safe.

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


def _is_generic_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    prefix = email.lower().strip().split("@")[0]
    # V24.4 (L5-4): Expanded generic email prefix filter. Previous set missed
    # common team inbox prefixes that are not decision-maker contacts.
    generics = {
        "support", "info", "admin", "sales", "billing", "jobs", "careers",
        "privacy", "help", "contact", "marketing", "office", "team", "hello",
        "enquiries", "enquiry", "noreply", "no-reply", "do-not-reply",
        "notifications", "alerts", "newsletter", "subscriptions", "listings",
        "rentals", "general", "accounts", "feedback", "service", "services",
    }
    return prefix in generics


# ---------------------------------------------------------------------------
# FIX 2: Atomic Exclusivity Lock Acquisition (transactional)
# ---------------------------------------------------------------------------
@_firestore_transactional
def _acquire_lead_lock(transaction, lock_ref, now_utc):
    """
    Atomically acquires a global exclusivity lock.
    Returns True  → lock acquired (new or expired).
    Returns False → domain within 3-day exclusivity window; caller skips.
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
    # Postmortem Fix #2: write expire_at alongside locked_until so the Firestore
    # TTL policy (once enabled in GCP Console on global_lead_locks.expire_at)
    # automatically cleans up stale lock documents.
    # V25.2.2: Reduced from 14 days → 3 days for faster lead re-discovery.
    _locked_until = now_utc + datetime.timedelta(days=3)
    transaction.set(lock_ref, {
        "locked_until": _locked_until,
        "expire_at":    _locked_until,   # TTL field — Firestore TTL policy key
    })
    return True


# ---------------------------------------------------------------------------
# Credit settlement — async Cloud Task to orchestrator
# ---------------------------------------------------------------------------

def _settle_credit(tenant_id: str, outcome: str, count: int = 1, lead_id: str = ""):
    """
    Non-blocking credit settlement via Cloud Task to orchestrator.
    lead_id is the idempotency key — the orchestrator atomically stamps
    credit_settled=True before applying the wallet Increment.
    Falls back to direct shard write if ORCHESTRATOR_URL is unset.

    SF-012 FIX: Added OIDC_token to the Cloud Task http_request.
    The orchestrator's /api/internal/credits/settle is protected by
    @require_tasks_oidc which cryptographically verifies a Google-signed JWT.
    Without OIDC_token, the orchestrator returns HTTP 401 and the credit
    task fails silently (non-fatal catch) — wallet never increments.
    With OIDC_token, Cloud Tasks mints a fresh OIDC JWT signed by
    ORCHESTRATOR_SA_EMAIL and attaches it as the Authorization: Bearer header
    before dispatching the HTTP request to the orchestrator.
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
        from google.cloud import tasks_v2 as _tv2
        tc         = get_tasks_client()
        queue_path = tc.queue_path(PROJECT_ID, LOCATION, QUEUE)
        body       = json.dumps({
            "tenant_id": tenant_id,
            "outcome":   outcome,
            "count":     count,
            "lead_id":   lead_id,
        }).encode()

        # Build the http_request dict
        settle_url = f"{ORCHESTRATOR_URL}/api/internal/credits/settle"
        http_req: dict = {
            "http_method": _tv2.HttpMethod.POST,
            "url":         settle_url,
            "headers":     {"Content-Type": "application/json"},
            "body":        body,
        }

        # SF-012 FIX: Attach OIDC token so orchestrator @require_tasks_oidc passes.
        # ORCHESTRATOR_SA_EMAIL must be the service account that has Cloud Run
        # Invoker permissions on the orchestrator service. If not configured,
        # we log a warning but still enqueue (task will 401 at orchestrator but
        # won't crash the pipeline).
        if ORCHESTRATOR_SA_EMAIL:
            http_req["oidc_token"] = {
                "service_account_email": ORCHESTRATOR_SA_EMAIL,
                "audience":              settle_url,
            }
        else:
            log.warning(
                "settle_credit_oidc_missing",
                tenant_id=tenant_id,
                note="ORCHESTRATOR_SA_EMAIL not set. Cloud Task will deliver without "
                     "OIDC token. Orchestrator will reject with 401 if OIDC enforced."
            )

        tc.create_task(parent=queue_path, task={"http_request": http_req})
        log.info("settle_credit_enqueued", tenant_id=tenant_id,
                 outcome=outcome, lead_id=(lead_id[:12] if lead_id else "N/A"),
                 oidc_configured=bool(ORCHESTRATOR_SA_EMAIL))
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

    if campaign.get("tenant_id") != tenant_id:
        log.warning("dispatch_unauthorized_tenant_context", campaign_id=campaign_id, tenant_id=tenant_id)
        return jsonify({"error": "Unauthorized tenant context"}), 403

    bio             = campaign.get("bio", "")
    sourcing_vector = campaign.get("sourcing_vector", "B2B")
    location        = campaign.get("location", "").strip()

    # V25.3.1: Adaptive threshold — campaigns with thin bios (< 50 chars)
    # get a lower bar because Gemini can't produce high-confidence scores
    # without rich ICP context. This prevents vague bios from killing
    # 100% of leads. The user gets a bio_quality_warning at campaign
    # creation to encourage better input.
    _raw_bio = (campaign.get("bio") or campaign.get("effective_bio") or "").strip()
    _is_thin_bio = len(_raw_bio) < 50

    # V24.6.1: Replace all bio assembly logic (persona vault + V24.6.0 keywords
    # fallback) with build_enriched_context(). The context builder aggregates
    # ALL 15+ campaign and persona fields into a structured ICP context.
    # This fixes pre-filter context starvation for ALL user types:
    #   - Power user (all fields filled): gets 8 rich context sections
    #   - Average user (bio + persona linked): gets persona + market context
    #   - Lazy user (name + location only): gets geo-targeted name context
    # The V24.6.0 keywords-as-bio fallback is preserved inside Layer 2 of the
    # builder, so there is no regression for campaigns without a persona.
    try:
        from services.context_builder import build_enriched_context  # type: ignore[import]
        bio = build_enriched_context(campaign)
        log.info(
            "dispatch_enriched_context_assembled",
            bio_chars=len(bio),
            campaign_id=campaign_id,
        )
    except Exception as _ctx_err:
        log.warning(
            "dispatch_context_builder_failed",
            campaign_id=campaign_id,
            error=str(_ctx_err),
            note="Falling back to raw bio field. Check context_builder.py.",
        )
        # Fallback: persona vault precedence (V23 behaviour)
        persona_id = campaign.get("persona_id", "")
        if persona_id:
            persona_bio = campaign.get("persona_bio", "").strip()
            if persona_bio:
                bio = persona_bio

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

    # ── Target personas — CAMPAIGN-LOCAL ONLY (V23 isolation enforcement) ────
    # FIX (2026-06-21): Removed live read-through to tenant_profiles collection.
    # The old code fell back to reading target_personas from the global Digital
    # Twin profile when the campaign document lacked them. This violated the
    # snapshot-copy isolation boundary — any B2B persona descriptions on the
    # tenant profile would silently leak into campaign-specific Prism evaluation.
    #
    # New policy:
    #   1. Campaign has target_personas → use them (snapshot from creation)
    #   2. Campaign lacks target_personas but has persona_bio → synthesize
    #      a single-entry persona from campaign-level fields (safe — snapshot)
    #   3. Campaign has neither → CRITICAL warning for human investigation;
    #      proceed with empty personas (PrismPipeline handles gracefully)
    # -----------------------------------------------------------------------
    raw_personas = campaign.get("target_personas", [])
    if not raw_personas:
        # Attempt synthesis from campaign-level persona snapshot fields
        _p_bio  = campaign.get("persona_bio", "").strip()
        _p_name = campaign.get("persona_name", "").strip()
        if _p_bio or bio:
            raw_personas = [{
                "name":          _p_name or "Target Persona",
                "description":   _p_bio or bio,
                "location_hint": location or "Global",
            }]
            log.info("dispatch_persona_synthesized_from_campaign",
                     campaign_id=campaign_id,
                     persona_name=raw_personas[0]["name"],
                     note="Synthesized from campaign-level persona_bio/bio. "
                          "No tenant_profiles read performed.")
        else:
            # No persona data at all — flag for human-in-the-loop review
            log.critical(
                "dispatch_campaign_missing_personas",
                campaign_id=campaign_id,
                tenant_id=tenant_id,
                has_bio=bool(bio),
                has_persona_bio=bool(_p_bio),
                has_persona_name=bool(_p_name),
                note="HUMAN-IN-THE-LOOP: Campaign has no target_personas, "
                     "no persona_bio, and no bio. Prism will operate in "
                     "generic mode. Investigate campaign configuration.",
            )
    campaign["target_personas"] = raw_personas

    # ── TRACE-5: Instantiate PrismPipeline ──────────────────────────────────
    log.info("TRACE-5: Instantiating PrismPipeline.", campaign_id=campaign_id,
             persona_count=len(raw_personas))
    prism = None
    try:
        # BUG-5 FIX: Use get_serper_key() DCL singleton instead of _get_secret().
        # _get_secret() made a live Secret Manager RPC on every /dispatch call.
        # get_serper_key() is cached for the container lifetime — one RPC ever.
        _serper_key = get_serper_key(SERPER_API_KEY_NAME).strip()
        prism       = PrismPipeline(campaign, _db(), _serper_key)
        log.info("dispatch_prism_instantiated", campaign_id=campaign_id,
                 persona_count=len(raw_personas))
    except Exception as prism_err:
        log.warning("dispatch_prism_init_failed", error=str(prism_err),
                    note="Falling back to scraper-heavy deferrals.")


    # ── TRACE-6: Destructive Queue Pop (Batch of 10) ────────────────────────
    current_queue = campaign.get("unprocessed_queue", [])
    if not current_queue:
        log.info("TRACE-6: unprocessed_queue empty — exiting gracefully.",
                 campaign_id=campaign_id)
        return jsonify({"status": "queue_empty", "processed": 0}), 200

    # BATCH_SIZE restored to 10 (P2 fix — 2026-06-20).
    # The prior reduction to 5 halved daily throughput to ~30 URLs/day per campaign.
    # At 10/dispatch × 4h intervals = max 60 URLs/day per campaign — adequate
    # pipeline fill rate given the downstream score gate pass-rate of ~30-40%.
    BATCH_SIZE = 10
    batch_urls = current_queue[:BATCH_SIZE]
    remaining  = current_queue[BATCH_SIZE:]

    # BUG-2 FIX: Atomic transactional queue pop — prevents double-dispatch race.
    # A bare .update({"unprocessed_queue": remaining}) is NOT atomic. If two
    # Cloud Task workers fire simultaneously for the same campaign_id (Cloud
    # Tasks guarantees at-least-once delivery), both read the same snapshot,
    # compute the same remaining slice, and the second .update() silently
    # overwrites the first — causing duplicate lead processing.
    # Fix: Use ArrayRemove inside a transaction. ArrayRemove is idempotent
    # and set-based — even if two workers race, each URL is only removed once.
    try:
        @_firestore_transactional
        def _pop_queue(txn, ref):
            txn.update(ref, {
                "unprocessed_queue": firestore.ArrayRemove(batch_urls)
            })
        _pop_txn = _db().transaction()
        _pop_queue(_pop_txn, campaign_ref)
    except Exception as pop_err:
        log.error("dispatch_queue_pop_failed", campaign_id=campaign_id,
                  error=str(pop_err), exc_info=True)
        return jsonify({"error": "Queue pop transaction failed"}), 500

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
    # V25.2.3 FIX: Align with produce.py shared_platforms. Previously missing
    # all buyer forum platforms — reddit, quora, HN, etc. collapsed to ONE
    # lead via domain-level dedup, silently dropping 90%+ of forum URLs.
    SHARED_PLATFORMS = {
        "linkedin.com", "medium.com", "substack.com", "wordpress.com", "github.io",
        # Buyer forum platforms — each thread/post is a unique lead (URL-path dedup)
        "reddit.com", "quora.com", "stackexchange.com", "stackoverflow.com",
        "news.ycombinator.com",
        "community.hubspot.com", "community.g2.com",
        "forum.growthackers.com", "indiehackers.com",
    }
    for batch_url in batch_urls:
        b_domain  = extract_root_domain(batch_url)
        is_social = any(b_domain.endswith(s) for s in SOCIAL_SET)
        is_shared = any(b_domain.endswith(s) for s in SHARED_PLATFORMS)
        # P3 FIX: B2C campaigns use URL-path dedup (matches produce.py cache_key)
        _is_b2c = _is_consumer_archetype(sourcing_vector)
        if is_social or is_shared or _is_b2c:
            parsed     = urlparse(batch_url)
            # V25.3.0: Include fragment (#review-0, etc.) for signal_harvest
            # review URLs that use fragments for per-review uniqueness.
            _frag = f"#{parsed.fragment}" if parsed.fragment else ""
            exact_path = f"{parsed.netloc}{parsed.path}{_frag}".lower().replace("www.", "")
            dedupe_target = exact_path
        else:
            dedupe_target = b_domain
        
        try:
            cache_key = hashlib.sha256(f"{tenant_id}_{dedupe_target}".encode()).hexdigest()
            cdoc = _db().collection("scraped_cache").document(cache_key).get()
            if cdoc.exists:
                cdata = cdoc.to_dict()
                txt = cdata.get("text", "")
                if txt:
                    # V25.3.0: Carry harvest metadata so the score gate can
                    # recognise pre-qualified signal_harvest leads and lower
                    # the accept_threshold accordingly.
                    snippet_map[batch_url] = {
                        "text":         txt,
                        "source":       cdata.get("source", ""),
                        "harvest_tier": cdata.get("harvest_tier", ""),
                    }

        except Exception as err:
            log.warning("dispatch_cache_lookup_error", url=batch_url[:80], error=str(err))

    # ── TRACE-7: Confidence Tiering Gate (pre_filter_gemini) ────────────────
    # V25.2.3 FIX: Buyer-signal platform bypass. Forum/community URLs are
    # intent-rich by nature — the 140-char snippet pre-filter is too
    # context-starved to judge them accurately (95% false rejection rate).
    # Auto-classify forum URLs as High-tier, only send non-forum URLs to Gemini.
    _PREFILTER_BYPASS_DOMAINS = {
        "reddit.com", "quora.com", "stackexchange.com", "stackoverflow.com",
        "news.ycombinator.com", "indiehackers.com", "community.hubspot.com",
        "community.g2.com", "forum.growthackers.com",
        "expatriates.com", "dubizzle.com", "olx.in", "olx.com",
        "mouthshut.com", "consumercomplaints.in",
    }
    bypass_urls  = []
    filter_urls  = []
    for u in batch_urls:
        u_domain = extract_root_domain(u)
        if any(u_domain.endswith(bp) for bp in _PREFILTER_BYPASS_DOMAINS):
            bypass_urls.append(u)
        else:
            filter_urls.append(u)

    if bypass_urls:
        log.info("prefilter_bypass_forum_urls",
                 count=len(bypass_urls),
                 note="V25.2.3: Forum/community URLs auto-classified High.")

    log.info("TRACE-7: Calling pre_filter_gemini.", url_count=len(filter_urls),
             bypassed=len(bypass_urls))

    if filter_urls:
        synthetic_snippets = [
            {"link": u, "snippet": snippet_map.get(u, {}).get("text", "") if isinstance(snippet_map.get(u), dict) else snippet_map.get(u, ""), "title": ""}
            for u in filter_urls
        ]
        try:
            with _cf.ThreadPoolExecutor(max_workers=1) as pool:
                fut    = pool.submit(pre_filter_gemini, synthetic_snippets, bio, location)
                tiered = fut.result(timeout=30)
        except Exception as _pf_err:
            log.warning("pre_filter_timeout_all_urls_approved",
                        error=str(_pf_err),
                        url_count=len(filter_urls),
                        note="V24.4 (L4-1): Pre-filter gate failed; ALL URLs treated as High-tier. "
                             "Velocity gate bypassed for this batch. Monitor for quality degradation.")
            tiered = {"High": filter_urls, "Medium": [], "Low": []}
    else:
        tiered = {"High": [], "Medium": [], "Low": []}

    # Merge bypass URLs into High tier
    tiered["High"] = bypass_urls + tiered.get("High", [])

    high_urls   = tiered.get("High", [])
    medium_urls = tiered.get("Medium", [])



    # Velocity gate (Medium URLs)
    # BUG-3 FIX: Replace deprecated positional where() with FieldFilter.
    # Positional where() is deprecated in google-cloud-firestore >= 2.13.
    velocity_threshold = VELOCITY_THRESHOLD
    try:
        cutoff_24h   = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        recent_count = (
            _db().collection("leads")
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .where(filter=FieldFilter("status",    "==", "new"))
            .where(filter=FieldFilter("createdAt", ">=", cutoff_24h))
            .count().get()[0][0].value
        )
    except Exception as _vel_err:
        # V24.4 (L4-2): Velocity gate disabled due to Firestore query failure.
        # All Medium URLs will pass regardless of actual volume.
        log.warning("velocity_gate_disabled_firestore_error",
                    error=str(_vel_err),
                    note="Medium URLs will pass without volume check. Monitor lead quality.")
        recent_count = 0

    allow_medium  = recent_count < velocity_threshold
    approved_urls = high_urls + (medium_urls if allow_medium else [])
    url_to_tier   = {u: "High" for u in high_urls}
    url_to_tier.update({u: "Medium" for u in medium_urls})
    log.info("TRACE-7: Gate complete.",
             high=len(high_urls), medium=len(medium_urls),
             approved=len(approved_urls), gate_rejected=len(batch_urls)-len(approved_urls))

    # ── TRACE-8: PRISM Processing Loop (parallel, per-URL 25s timeout) ─────────
    # BUG-1 + BUG-4 FIX: Run each URL concurrently via ThreadPoolExecutor.
    # Each future has a hard 25s wall-clock timeout:
    #   - prism.process_url(): up to 14s (4s connect + 10s read per httpx call)
    #   - deep_context_serper_dork(): up to 10s (3 parallel Serper calls)
    # Total per-URL budget: 25s. Batch of 10 with max 5 workers runs in ~50s,
    # safely under the 120s Gunicorn timeout and 540s Cloud Run request timeout.
    # Previously: fully synchronous → 140s+ for 10 URLs → Gunicorn kill → 502.
    log.info("TRACE-8: Entering PRISM processing loop (parallel).",
             url_count=len(approved_urls))
    all_results    = []
    scrape_success = scrape_failed = 0
    _ENTITY_DOMAIN_COUNTS.clear()  # V26.0: Reset per-domain rate limiter per batch

    def _process_single_url(url: str) -> dict:
        """Process one URL through lock → dedup → PRISM → Gemini → Firestore.
        Returns a status dict. Exceptions are caught and returned as errors.
        """
        target_domain = extract_root_domain(url)
        if not target_domain:
            log.info("dispatch_skip_no_domain", url=url[:80],
                     note="Could not extract root domain from URL.")
            return {"url": url, "status": "skip_no_domain"}

        # V24.5.7: Pre-PRISM TLD gate. Non-business TLD domains (academic, government,
        # personal blog) were previously gated only inside deep_context_serper_dork()
        # AFTER PRISM already ran (costing 3-5 credits per URL). This gate fires before PRISM
        # so those credits are never spent.
        # V25.3.0: .org REMOVED from block list. Many legitimate B2B/SaaS
        # companies use .org (e.g. trade-associations-as-prospects, open-source
        # orgs with commercial arms). The blanket block caused ~15-20% false
        # negatives in nonprofit-adjacent verticals. Nonprofits that aren't
        # prospects are filtered out downstream by the scoring model.
        _NON_BUSINESS_TLD_GATE = (
            ".edu", ".ac.in", ".ernet.in", ".gov", ".gov.in", ".mil",
            ".blog",  # personal/corporate blog hosting TLD
            ".dev",   # personal developer portfolios
            ".page",  # Google Sites personal pages
        )
        if any(target_domain.endswith(tld) for tld in _NON_BUSINESS_TLD_GATE):
            log.info(
                "dispatch_pre_prism_tld_gate",
                url=url[:80],
                domain=target_domain,
                note="Non-business TLD blocked before PRISM. Saves 3-8 credits vs enrichment-stage gate.",
            )
            return {"url": url, "status": "skip_non_business_tld"}

        is_social = any(target_domain.endswith(s) for s in SOCIAL_SET)
        is_shared = any(target_domain.endswith(s) for s in SHARED_PLATFORMS)
        # P3 FIX: B2C/Real Estate campaigns use URL-path dedup (matches produce.py)
        _is_b2c = _is_consumer_archetype(sourcing_vector)
        if is_social or is_shared or _is_b2c:
            parsed     = urlparse(url)
            # V25.3.0: Include fragment for signal_harvest review URL uniqueness
            _frag_per_url = f"#{parsed.fragment}" if parsed.fragment else ""
            exact_path = f"{parsed.netloc}{parsed.path}{_frag_per_url}".lower().replace("www.", "")
            lock_entity   = hashlib.sha256(exact_path.encode()).hexdigest()
            dedupe_target = exact_path
        else:
            lock_entity   = target_domain
            dedupe_target = target_domain

        # ── Global Exclusivity Lock ──────────────────────────────────────────
        lock_ref = _db().collection("global_lead_locks").document(lock_entity)
        try:
            now_utc  = datetime.datetime.now(datetime.timezone.utc)
            lock_txn = _db().transaction()
            acquired = _acquire_lead_lock(lock_txn, lock_ref, now_utc)
            if not acquired:
                log.info("dispatch_exclusivity_skip", url=url[:80],
                         lock_entity=lock_entity, note="3-day window active.")
                return {"url": url, "status": "skip_exclusivity"}
        except Exception as lock_err:
            log.warning("dispatch_lock_failed", url=url[:80], error=str(lock_err))
            return {"url": url, "status": "skip_lock_error"}

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
                "processing_started_at": firestore.SERVER_TIMESTAMP,
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
            return {"url": url, "status": "skip_dup"}

        try:
            # ── STEP 1: PRISM deep scrape FIRST (P0 FIX — 2026-06-20) ────────────
            # ARCHITECTURE FIX: Invert the two-stage funnel.
            # Previously: snippet → score(snippet) → if ≥6 → PRISM scrape
            # Now:         PRISM scrape → score(full DOM) → dynamic threshold
            #
            # The old flow evaluated ~200 chars of Serper snippet through Gemini,
            # which killed 60-75% of viable B2C leads at the score < 6 gate because
            # snippets lack explicit intent language. Scoring the full DOM gives
            # Gemini sufficient context to accurately assess lead quality.
            text, tech_stack, emails, phones = "", [], [], []
            prism_mode, fallback_used = "legacy", False

            if prism is not None:
                hook          = prism.process_url(url, tenant_id)
                text          = hook.get("text", "")
                tech_stack    = hook.get("tech_stack", [])
                emails        = hook.get("emails", [])
                phones        = hook.get("phones", [])
                prism_mode    = hook.get("mode", "GeneralDomain")
                fallback_used = hook.get("fallback_used", False)
            else:
                _defer_to_scraper_heavy(url, lead_id, tenant_id, campaign_id,
                                        bio, target_domain, preferences_weights)
                return {"url": url, "status": "deferred_prism_uninit"}

            # If PRISM returned empty text (JS SPA, WAF block), fall back to
            # the snippet cache from the produce phase.
            if not text:
                try:
                    cache_snap = _db().collection("scraped_cache").document(lead_id).get()
                    if cache_snap.exists:
                        text = cache_snap.to_dict().get("text", "")
                except Exception as cache_err:
                    log.warning("dispatch_snippet_fetch_failed", lead_id=lead_id, error=str(cache_err))

            if not text:
                # Neither PRISM nor snippet cache yielded usable text.
                # Defer to scraper-heavy for JS rendering instead of hard-deleting.
                log.info("dispatch_no_text_defer", url=url[:80], lead_id=lead_id,
                         prism_mode=prism_mode)
                _defer_to_scraper_heavy(url, lead_id, tenant_id, campaign_id,
                                        bio, target_domain, preferences_weights)
                return {"url": url, "status": "deferred_no_text"}

            # WAF / bot-check detection on full DOM
            bot_keywords = ["Cloudflare Ray ID", "Please verify you are human",
                            "Enable JavaScript and cookies to continue",
                            "Checking if the site connection is secure",
                            "Access Denied", "403 Forbidden"]
            _is_waf_text = any(kw.lower() in text.lower() for kw in bot_keywords)
            # V25.3.1: WAF-blocked pages should defer to scraper-heavy (headless
            # browser) instead of hard-failing. PRISM's GeneralDomainHook may have
            # hit WAF but scraper-heavy has Playwright/headless Chrome.
            if _is_waf_text:
                log.info("dispatch_waf_deferred", url=url[:80],
                         note="WAF detected in DOM text. Deferring to scraper-heavy.")
                _defer_to_scraper_heavy(url, lead_id, tenant_id, campaign_id,
                                        bio, target_domain, preferences_weights)
                return {"url": url, "status": "deferred_waf"}

            # Stamp prism_mode on stub
            try:
                doc_ref.update({"prism_mode": prism_mode, "fallback_used": fallback_used})
            except Exception:
                pass

            # ── V26.0: Strategy-Aware Processing Branch ──────────────────────────
            # PLATFORM_MINING and COMPETITOR_TOUCHPOINT strategies use entity
            # extraction instead of text scoring. One aggregator page → N leads.
            _intel_strategy = campaign.get("intelligence_strategy", {})
            _primary_strategy = _intel_strategy.get("primary", "")
            if _primary_strategy in ("PLATFORM_MINING", "COMPETITOR_TOUCHPOINT") and text:
                _icp_context = campaign.get("effective_bio") or campaign.get("bio") or ""
                _icp_context += " | " + (campaign.get("keywords") or "")
                log.info("dispatch_entity_extraction_branch",
                         url=url[:80], strategy=_primary_strategy,
                         text_chars=len(text), lead_id=lead_id)

                extracted = _extract_entities_from_dom(
                    text=text,
                    icp_context=_icp_context,
                    campaign=campaign,
                    source_url=url,
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    lead_id=lead_id,
                )

                if extracted:
                    # Update the original stub lead to mark it as an extraction source
                    doc_ref.update({
                        "status": "entity_extraction_source",
                        "entity_count": len(extracted),
                        "origin_engine": "entity_extractor",
                        "prism_mode": "entity_extraction",
                        "updatedAt": firestore.SERVER_TIMESTAMP,
                    })
                    try:
                        _settle_credit(tenant_id, "success", lead_id=lead_id)
                    except Exception as _settle_err:
                        log.warning("settle_credit_entity_branch_failed",
                                    lead_id=lead_id, error=str(_settle_err))
                    log.info("dispatch_entity_extraction_success",
                             url=url[:80], entities_created=len(extracted),
                             strategy=_primary_strategy)
                    return {
                        "url": url,
                        "status": "entity_extraction_complete",
                        "entities": len(extracted),
                    }
                else:
                    # Entity extraction yielded nothing — fall through to normal
                    # scoring pipeline as fallback.
                    log.info("dispatch_entity_extraction_fallthrough",
                             url=url[:80], strategy=_primary_strategy,
                             note="Entity extraction returned 0 entities. "
                                  "Falling through to standard scoring.")

            # ── STEP 2: Deep web enrichment ──────────────────────────────────────
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
                # SCORE-04 FIX: Preserve the lead with rlhf_filtered status instead
                # of deleting it. This provides visibility into RLHF-dropped leads
                # for operator review and model debugging.
                doc_ref.update({
                    "status": "rlhf_filtered",
                    "rlhf_fit_score": fit_score,
                    "rlhf_filter_reason": (
                        f"RLHF fit_score {fit_score} <= -3 threshold. "
                        f"Domain: {target_domain}. "
                        f"Hiring intent: {native_hiring_intent}. "
                        f"Tech stack: {tech_stack[:5]}."
                    ),
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                })
                try:
                    _db().collection("global_lead_locks").document(lock_entity).delete()
                except Exception as _lock_del_err:
                    log.error("lead_lock_delete_failed",
                              lock_entity=lock_entity,
                              url=url[:80] if url else "unknown",
                              error=str(_lock_del_err))
                # FIN-03: Settle credit on RLHF drop path
                try:
                    _settle_credit(tenant_id, "failure", lead_id=lead_id)
                except Exception as _settle_err:
                    log.warning("settle_credit_on_rlhf_drop_failed",
                                lead_id=lead_id, error=str(_settle_err))
                return {"url": url, "status": "rlhf_drop"}

            # ── STEP 3: Score on FULL DOM text (P0 + P1 FIX) ─────────────────────
            try:
                # Increment usage metrics for Gemini call
                shard_id = random.randint(0, 9)
                _db().collection("usage_metrics").document(tenant_id) \
                    .collection("shards").document(str(shard_id)) \
                    .set({"gemini_calls": firestore.Increment(1)}, merge=True)

                log.info("TRACE-9: Calling final_score_and_dm on full DOM.",
                         url=url[:80], text_chars=len(text), prism_mode=prism_mode)
                evaluation = final_score_and_dm(
                    text=text,
                    active_campaigns=active_campaigns,
                    context_payload=context_payload,
                    tech_stack=tech_stack,
                    source_url=url
                )
            except TimeoutError:
                doc_ref.update({"status": "failed", "error": "Vertex AI timeout"})
                try:
                    _db().collection("global_lead_locks").document(lock_entity).delete()
                except Exception as _lock_del_err:
                    log.error("lead_lock_delete_failed",
                              lock_entity=lock_entity,
                              url=url[:80] if url else "unknown",
                              error=str(_lock_del_err))
                # FIN-03: Settle credit on Vertex AI timeout
                try:
                    _settle_credit(tenant_id, "failure", lead_id=lead_id)
                except Exception as _settle_err:
                    log.warning("settle_credit_on_timeout_failed",
                                lead_id=lead_id, error=str(_settle_err))
                return {"url": url, "status": "failed_vertex_timeout"}
            except Exception as eval_err:
                doc_ref.update({"status": "failed", "error": f"Scoring failed: {eval_err}"})
                try:
                    _db().collection("global_lead_locks").document(lock_entity).delete()
                except Exception as _lock_del_err:
                    log.error("lead_lock_delete_failed",
                              lock_entity=lock_entity,
                              url=url[:80] if url else "unknown",
                              error=str(_lock_del_err))
                # FIN-03: Settle credit on eval failure
                try:
                    _settle_credit(tenant_id, "failure", lead_id=lead_id)
                except Exception as _settle_err:
                    log.warning("settle_credit_on_eval_failed",
                                lead_id=lead_id, error=str(_settle_err))
                return {"url": url, "status": "failed_eval"}

            score = evaluation.get("score", 0)

            # V24.6.0: Page-type score cap — structural URL signals that
            # unambiguously identify non-buyer page categories regardless of
            # what Gemini scores. Prevents conference pages, government portals,
            # and academic repositories from scoring 9-10 and reaching the feed.
            # Applied BEFORE the thin-payload threshold so the cap takes
            # precedence over everything.
            #
            # Evidence: postgresconf.org/conferences/SV2022/.../sql-engine → 10/10
            #           DeptofCommerceIndia → 9/10
            # Both are correct Gemini keyword matches but structurally wrong page types.
            import re as _re_pt
            _PAGE_TYPE_CAP: int | None = None
            _url_lower = url.lower()
            _PAGE_TYPE_RULES: list[tuple[str, int, str]] = [
                # (pattern, score_cap, label)
                # Conference / event pages
                (r"/(conference|conf|summit|symposium|webinar|event)s?/",           3, "conference_page"),
                (r"/(program|schedule|agenda|speakers?|sessions?)/proposals?",       3, "conference_program"),
                # Government / regulatory portals
                (r"\.(gov|mil|govt)\.?",                                            2, "government_portal"),
                (r"/(ministry|department|dept)[-_/]",                               2, "government_dept"),
                # Academic repositories and research
                (r"/(research|paper|abstract|thesis|dissertation|preprint)/",       3, "academic_content"),
                (r"/(sol3|ssrn|arxiv|researchgate|pubmed)/",                        2, "academic_repo"),
                # Press release / news wire
                (r"/(press[-_]release|press[-_]room|newsroom|media[-_]centre)/",    4, "press_release"),
                # Job boards (hiring page ≠ buyer page)
                (r"/(jobs|careers|vacancies|join[-_]us)/",                          4, "job_board"),
            ]
            for _pattern, _cap, _label in _PAGE_TYPE_RULES:
                if _re_pt.search(_pattern, _url_lower):
                    _PAGE_TYPE_CAP = _cap
                    log.info(
                        "dispatch_page_type_cap",
                        url=url[:80],
                        original_score=score,
                        cap=_cap,
                        label=_label,
                        note="Structural page type cap applied before score gate.",
                    )
                    break
            if _PAGE_TYPE_CAP is not None and score > _PAGE_TYPE_CAP:
                score = _PAGE_TYPE_CAP

            # ── STEP 3b: Dynamic score threshold (P1 FIX — 2026-06-20) ───────────
            # Restored from legacy main.py L2820-2828.
            # Snippet-sourced leads (< 500 chars) lack DOM depth. Gemini cannot
            # confidently score them >= 7 even with clear intent.
            # WalledGardenHook tags thin payloads with [SHADOW_LEARNER_THIN_PAYLOAD]
            # prefix — also treated as thin regardless of char count.
            _is_shadow_thin  = text.strip().startswith("[SHADOW_LEARNER_THIN_PAYLOAD]")
            is_thin_payload  = _is_shadow_thin or len(text.strip()) < 500

            # V25.3.0: Harvest-enriched leads were already pre-qualified by
            # inline_score_signal() with full content. Double-scoring the same
            # content at a 7-threshold causes ~50% attrition on pre-qualified leads.
            _snippet_entry   = snippet_map.get(url, {})
            _snippet_meta    = _snippet_entry if isinstance(_snippet_entry, dict) else {}
            is_harvest_lead  = (
                _snippet_meta.get("source") == "signal_harvest"
                or _snippet_meta.get("harvest_tier") in ("HIGH", "MEDIUM")
            )
            if is_harvest_lead:
                accept_threshold = 5   # Pre-qualified by inline scoring
            elif _is_thin_bio:
                accept_threshold = 4   # V25.3.1: Vague bio — Gemini can't score > 5
            elif is_thin_payload:
                accept_threshold = 6
            else:
                accept_threshold = 7

            log.info("dispatch_score_gate_eval",
                     url=url[:80], score=score, threshold=accept_threshold,
                     text_chars=len(text), thin=is_thin_payload,
                     shadow_thin=_is_shadow_thin, harvest_lead=is_harvest_lead,
                     prism_mode=prism_mode)

            if score < accept_threshold:
                log.info("dispatch_score_gate_drop", url=url[:80],
                         score=score, threshold=accept_threshold)
                doc_ref.delete()
                try:
                    _db().collection("global_lead_locks").document(lock_entity).delete()
                except Exception as _lock_del_err:
                    log.error("lead_lock_delete_failed",
                              lock_entity=lock_entity,
                              url=url[:80] if url else "unknown",
                              error=str(_lock_del_err))
                # V24.4 (L4-7): Settle credit on score-drop to prevent reserved-but-
                # never-settled accounting gaps. /finalize already does this; now
                # the primary dispatch path matches that behaviour.
                _settle_credit(tenant_id, "failure", lead_id=lead_id)
                return {"url": url, "status": "score_drop", "score": score}

            # ── STEP 4: Consolidate lead details and save into root leads collection ──
            # V24.4 (L4-5): Medium-tier URLs with snippet-only text (< 300 chars)
            # are marked enrichment_pending rather than scored on insufficient data.
            # Scoring a 2-sentence snippet populates all fields as 'Unknown' and
            # destroys the lead's value to the operator.
            _snippet_len = len(text.strip()) if text else 0
            if url_to_tier.get(url) == "Medium" and _snippet_len < 300:
                doc_ref.update({
                    "status": "enrichment_pending",
                    "enrichment_reason": "Medium-tier URL with snippet-only text; awaiting full scrape.",
                    "confidence_tier": "Medium",
                    "source_url": url,
                    "tenant_id": tenant_id,
                    "campaign_id": campaign_id,
                })
                log.info("dispatch_medium_enrichment_pending",
                         url=url[:80], snippet_len=_snippet_len)
                return {"url": url, "status": "enrichment_pending"}

            contact_endpoints = []
            for e in list(evaluation.get("contact_endpoints", [])):
                if e.get("platform") == "email" and _is_generic_email(e.get("uri", "")):
                    continue
                # SCORE-01 FIX: Validate email contacts against scraped DOM text.
                # Mark each contact with a 'validated' boolean indicating whether
                # the email address actually appears in the scraped page content.
                # Unvalidated contacts are kept but flagged for operator awareness.
                if e.get("platform") == "email" and e.get("uri"):
                    e["validated"] = bool(text and e["uri"].lower() in text.lower())
                else:
                    e["validated"] = True  # Non-email contacts default to validated
                contact_endpoints.append(e)
            existing_uris     = {e["uri"] for e in contact_endpoints}
            for em in (emails or [])[:3]:
                if em and em not in existing_uris and not _is_generic_email(em):
                    _em_validated = bool(text and em.lower() in text.lower())
                    contact_endpoints.append({"platform": "email", "uri": em, "validated": _em_validated})
                    existing_uris.add(em)
            for ph in (phones or [])[:2]:
                if ph and ph not in existing_uris:
                    contact_endpoints.append({"platform": "other", "uri": ph, "validated": True})
                    existing_uris.add(ph)

            # SCORE-02 FIX: All-unknown quality gate.
            # If score passes threshold but all key fields are unknown/empty,
            # the lead is too thin for the operator. Set enrichment_pending
            # so it can be re-processed with richer data.
            _company_unknown = (
                not evaluation.get("company_name")
                or str(evaluation.get("company_name", "")).strip().lower() in ("", "unknown", "none", "n/a")
            )
            _pain_unknown = (
                not evaluation.get("pain_point")
                or str(evaluation.get("pain_point", "")).strip().lower() in ("", "unknown", "none", "n/a")
            )
            _contacts_empty = len(contact_endpoints) == 0

            if _company_unknown and _pain_unknown and _contacts_empty:
                log.info(
                    "dispatch_all_unknown_quality_gate",
                    url=url[:80],
                    score=score,
                    company=evaluation.get("company_name"),
                    pain=evaluation.get("pain_point"),
                    contacts=len(contact_endpoints),
                    note="Score passes threshold but all key fields are unknown/empty. "
                         "Setting enrichment_pending.",
                )
                doc_ref.update({
                    "status": "enrichment_pending",
                    "enrichment_reason": (
                        "All-unknown quality gate: company_name, pain_point, and "
                        "contact_endpoints are all empty/unknown despite passing score gate."
                    ),
                    "score": score,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                })
                return {"url": url, "status": "enrichment_pending_all_unknown", "score": score}

            log.info("TRACE-10: Writing qualified lead to Firestore.",
                     url=url[:80], score=score, campaign_id=campaign_id)
            lead_payload = {
                "id":                           lead_id,
                "source_url":                   url,
                "tenant_id":                    tenant_id,
                "origin_engine":                "cartographer",
                "score":                        score,
                # V24.2 (L5-1): normalized_score (0-100) unifies outbound (×10)
                # and inbound (×100 from 0-1 intent_score) onto the same scale.
                # The UI must read normalized_score for display.
                "normalized_score":             min(score * 10, 100),
                "matched_campaign_ids":         evaluation.get("matched_campaign_ids", []),
                "matched_campaigns":            [campaign_id],
                "campaign_id":                  campaign_id,
                "trend_mapped":                 evaluation.get("trend_mapped", False),
                "highest_campaign_id":          evaluation.get("highest_campaign_id", "Unknown"),
                "pain_point":                   evaluation.get("pain_point", ""),
                "dm":                           evaluation.get("dm", ""),
                "intent_signal":                evaluation.get("intent_signal", ""),
                "hiring_intent_found":          evaluation.get("hiring_intent_found", "No"),
                "tech_stack_found":             tech_stack,
                "icebreaker_angle":             evaluation.get("icebreaker_angle", ""),
                "contact_endpoints":            contact_endpoints,
                "decision_maker_name":          evaluation.get("decision_maker_name", "Unknown"),
                "decision_maker_title":         evaluation.get("decision_maker_title", "Unknown"),
                "company_size_tier":            evaluation.get("company_size_tier", "Unknown"),
                "primary_objection_hypothesis": evaluation.get("primary_objection_hypothesis", "Unknown"),
                "company_name":                 evaluation.get("company_name"),
                "dossier_text":                 None,
                # V24.0: Explainable Scoring fields
                "score_reasoning":              evaluation.get("score_reasoning", ""),
                "confidence_level":             evaluation.get("confidence_level", "SPECULATIVE"),
                "evidence_chain":               evaluation.get("evidence_chain", []),
                "sourcing_vector":              sourcing_vector,
                "confidence_tier":              url_to_tier.get(url, "High"),
                "prism_mode":                   prism_mode,
                "prism_fallback":               fallback_used,
                "status":                       "new",
                "is_in_crm":                    False,
                # V25.2.0: Social signal provenance (populated for social-snippet leads)
                "signal_source_type":           "full_text",
                "signal_platform":              "",
                "social_snippet":               "",
                # V25.2.0: Cluster metadata (cluster leads are written by signal_cluster_analyst.py)
                "is_cluster_lead":              False,
                "cluster_id":                   "",
                "convergence_score":            0.0,
                "cluster_signals":              [],
                "cluster_snippets":             [],
                "cluster_platforms":            [],
                "cluster_summary":              "",
                "source_diversity":             0,
                "cluster_label":                "",
                # Phase 3D / Phase 4A: source_type from snippet_map enables
                # downstream routing (signal_harvest, organic, etc.)
                "source_type":                  _snippet_meta.get("source", "organic"),
            }
            doc_ref.set(lead_payload, merge=True)
            _settle_credit(tenant_id, "success", lead_id=lead_id)
            
            # Shadow Track BQ write for statistical RLHF routing
            _shadow_track_bq(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                lead_id=lead_id,
                url=url,
                score=score,
                sourcing_vector=sourcing_vector,
                pain_point=evaluation.get("pain_point", ""),
                prism_mode=prism_mode,
            )

            # V24.0: Intelligence Mesh enrichment (non-blocking, 3s timeout)
            try:
                from services.serper_service import search_serper  # type: ignore[import]
                company_for_mesh = evaluation.get("company_name")
                domain_for_mesh = extract_root_domain(url) if url else None
                if domain_for_mesh:
                    mesh_signals = enrich_signals(
                        company_name=company_for_mesh,
                        domain=domain_for_mesh,
                        serper_fn=search_serper,
                        timeout_s=3.0,
                    )
                    if mesh_signals:
                        for i, sig in enumerate(mesh_signals[:5]):
                            _db().collection("leads").document(lead_id) \
                                .collection("signals").document(f"mesh_{i}") \
                                .set({
                                    **sig,
                                    "campaign_id": campaign_id,
                                    "detected_at": datetime.datetime.now(datetime.timezone.utc),
                                }, merge=True)
                        log.info("mesh_signals_written", lead_id=lead_id,
                                 signal_count=len(mesh_signals))
            except Exception as mesh_err:
                log.warning("mesh_enrichment_failed", lead_id=lead_id,
                           error=str(mesh_err))

            if score >= 8:
                _maybe_notify_whatsapp(tenant_id, url, lead_id, score, evaluation)
            return {"url": url, "score": score, "status": "success"}

        except Exception as loop_err:
            log.error("dispatch_loop_crash", url=url[:80], campaign_id=campaign_id,
                      error=str(loop_err), exc_info=True)
            try:
                doc_ref.update({"status": "failed", "error": "Consumer pipeline crash"})
            except Exception as _stub_err:
                log.warning("lead_stub_update_failed",
                            lead_id=lead_id,
                            error=str(_stub_err))
            try:
                _db().collection("global_lead_locks").document(lock_entity).delete()
            except Exception as _lock_del_err:
                log.error("lead_lock_delete_failed",
                          lock_entity=lock_entity,
                          url=url[:80] if url else "unknown",
                          error=str(_lock_del_err))
            # FIN-03: Settle credit on pipeline crash path
            try:
                _settle_credit(tenant_id, "failure", lead_id=lead_id)
            except Exception as _settle_err:
                log.warning("settle_credit_on_crash_failed",
                            lead_id=lead_id, error=str(_settle_err))
            return {"url": url, "status": "crash"}

    # ── Dispatch all approved URLs in parallel (max 5 workers, 25s per URL) ─
    _URL_TIMEOUT_S = 25   # per-URL hard wall-clock ceiling
    _MAX_WORKERS   = 5    # Cloud Run 1 vCPU: 5 threads max before I/O queuing
    _OUTER_TIMEOUT_S = 180  # hard ceiling for entire URL batch (V23.7)

    with _cf.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_process_single_url, u): u for u in approved_urls}
        _batch_start = time.monotonic()
        _processed_urls: set[str] = set()  # V25.3.1: Track processed for batch-timeout cleanup
        for fut in _cf.as_completed(futures, timeout=(_URL_TIMEOUT_S * len(approved_urls))):
            if time.monotonic() - _batch_start > _OUTER_TIMEOUT_S:
                log.error("dispatch_batch_timeout_ceiling",
                          timeout_s=_OUTER_TIMEOUT_S,
                          processed=len(all_results),
                          remaining=len(futures) - len(_processed_urls),
                          note="Hard 180s ceiling hit. Remaining URLs abandoned.")
                break
            url = futures[fut]
            _processed_urls.add(url)
            try:
                result = fut.result(timeout=_URL_TIMEOUT_S)
                status = result.get("status", "unknown")
                if status == "success":
                    scrape_success += 1
                    all_results.append(result)
                elif status in ("failed_scrape", "failed_waf", "failed_vertex_timeout",
                                "failed_eval", "crash"):
                    scrape_failed += 1
            except _cf.TimeoutError:
                log.error("dispatch_url_timeout", url=url[:80],
                          timeout_s=_URL_TIMEOUT_S,
                          note="URL processing exceeded wall-clock ceiling.")
                # V25.3.1: Clean up orphaned resources on timeout
                try:
                    _p = urlparse(url)
                    _frag_t = f"#{_p.fragment}" if _p.fragment else ""
                    if any(_p.netloc.endswith(s) for s in SOCIAL_SET) or any(_p.netloc.endswith(s) for s in SHARED_PLATFORMS) or _is_consumer_archetype(sourcing_vector):
                        _ep = f"{_p.netloc}{_p.path}{_frag_t}".lower().replace("www.", "")
                    else:
                        _ep = extract_root_domain(url)
                    _lid = hashlib.sha256(f"{tenant_id}_{_ep}".encode()).hexdigest()
                    _db().collection("leads").document(_lid).update(
                        {"status": "failed", "error": "Dispatch per-URL timeout (25s)"}
                    )
                    _lock_ent = hashlib.sha256(_ep.encode()).hexdigest() if _ep != extract_root_domain(url) else extract_root_domain(url)
                    _db().collection("global_lead_locks").document(_lock_ent).delete()
                except Exception as _cleanup_err:
                    log.warning("dispatch_timeout_cleanup_failed", url=url[:80],
                                error=str(_cleanup_err))
                # FIN-03: Settle credit on per-URL timeout
                try:
                    _settle_credit(tenant_id, "failure", lead_id=_lid)
                except Exception as _settle_err:
                    log.warning("settle_credit_on_url_timeout_failed",
                                url=url[:80], error=str(_settle_err))
                scrape_failed += 1
            except Exception as fut_err:
                log.error("dispatch_future_crash", url=url[:80], error=str(fut_err),
                          exc_info=True)
                scrape_failed += 1

        # V25.3.1: Mark remaining unprocessed URLs' stubs as failed after batch timeout
        _remaining_urls = [u for u in approved_urls if u not in _processed_urls]
        for _remaining_url in _remaining_urls:
            try:
                _rp = urlparse(_remaining_url)
                _rfrag = f"#{_rp.fragment}" if _rp.fragment else ""
                if any(_rp.netloc.endswith(s) for s in SOCIAL_SET) or any(_rp.netloc.endswith(s) for s in SHARED_PLATFORMS) or _is_consumer_archetype(sourcing_vector):
                    _rep = f"{_rp.netloc}{_rp.path}{_rfrag}".lower().replace("www.", "")
                else:
                    _rep = extract_root_domain(_remaining_url)
                _rlid = hashlib.sha256(f"{tenant_id}_{_rep}".encode()).hexdigest()
                _db().collection("leads").document(_rlid).update(
                    {"status": "failed", "error": "Batch timeout — URL not processed"}
                )
                _rlock = hashlib.sha256(_rep.encode()).hexdigest() if _rep != extract_root_domain(_remaining_url) else extract_root_domain(_remaining_url)
                _db().collection("global_lead_locks").document(_rlock).delete()
            except Exception as _batch_cleanup_err:
                log.warning("dispatch_batch_timeout_cleanup_failed",
                            url=_remaining_url[:80],
                            error=str(_batch_cleanup_err))
            # FIN-03: Settle credit on batch timeout remaining URLs
            try:
                _settle_credit(tenant_id, "failure", lead_id=_rlid)
            except Exception as _settle_err:
                log.warning("settle_credit_on_batch_timeout_failed",
                            url=_remaining_url[:80], error=str(_settle_err))

    log.info("dispatch_batch_complete", campaign_id=campaign_id,
             processed=len(all_results), scrape_success=scrape_success,
             scrape_failed=scrape_failed)

    # ── Phase 3D: Score Distribution Monitoring ──────────────────────────────
    # Collect all scores from results that went through the scoring gate
    # (success, score_drop, enrichment_pending_all_unknown all carry a score).
    _all_scores = [
        r["score"] for r in all_results if "score" in r
    ]
    # Also scan futures results that weren't appended to all_results
    # (score_drop results are NOT in all_results — they were not "success").
    # Re-scan completed futures to capture score_drop results.
    for _fut_done in futures:
        try:
            _fr = _fut_done.result(timeout=0)
            if "score" in _fr and _fr not in all_results:
                _all_scores.append(_fr["score"])
        except Exception:
            pass  # Already handled above; skip

    _total_scored = len(_all_scores)
    if _total_scored > 0:
        _count_9_10 = sum(1 for s in _all_scores if s >= 9)
        _count_7_8  = sum(1 for s in _all_scores if 7 <= s <= 8)
        _count_5_6  = sum(1 for s in _all_scores if 5 <= s <= 6)
        _count_3_4  = sum(1 for s in _all_scores if 3 <= s <= 4)
        _count_1_2  = sum(1 for s in _all_scores if 1 <= s <= 2)
        _avg_score  = sum(_all_scores) / _total_scored
        _leads_created = scrape_success
        _leads_dropped = _total_scored - _leads_created

        log.info(
            "dispatch_score_distribution",
            campaign_id=campaign_id,
            total_scored=_total_scored,
            score_9_10=_count_9_10,
            score_7_8=_count_7_8,
            score_5_6=_count_5_6,
            score_3_4=_count_3_4,
            score_1_2=_count_1_2,
            avg_score=round(_avg_score, 2),
            leads_created=_leads_created,
            leads_dropped=_leads_dropped,
        )

        # Unhealthy distribution warnings
        _high_pct = (_count_9_10 + _count_7_8) / _total_scored
        if _high_pct > 0.60:
            log.warning(
                "dispatch_score_inflation",
                campaign_id=campaign_id,
                total_scored=_total_scored,
                pct_8_plus=round(_high_pct * 100, 1),
                avg_score=round(_avg_score, 2),
                note=">60% of scores are 8+. Scoring model may be uncalibrated.",
            )

        _low_pct = sum(1 for s in _all_scores if s < 5) / _total_scored
        if _low_pct > 0.80:
            log.warning(
                "dispatch_score_deflation",
                campaign_id=campaign_id,
                total_scored=_total_scored,
                pct_below_5=round(_low_pct * 100, 1),
                avg_score=round(_avg_score, 2),
                note=">80% of scores are below 5. Scoring model may be too strict.",
            )

        if _leads_created == 0 and _total_scored >= 5:
            log.warning(
                "dispatch_zero_conversion",
                campaign_id=campaign_id,
                total_scored=_total_scored,
                avg_score=round(_avg_score, 2),
                leads_created=0,
                leads_dropped=_leads_dropped,
                note="0 leads created from 5+ scored URLs. Funnel is broken.",
            )

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
    sourcing_vector = lead_data.get("sourcing_vector", "B2B")
    active_campaigns = [lead_data]  # minimal context for scoring

    if not text:
        doc_ref.update({"status": "failed_scrape",
                        "error": "scraper-heavy returned empty text"})
        lock_entity = lead_data.get("lock_entity")
        if lock_entity:
            try:
                _db().collection("global_lead_locks").document(lock_entity).delete()
            except Exception as _lock_del_err:
                log.error("lead_lock_delete_failed",
                          lock_entity=lock_entity,
                          url=url[:80] if url else "unknown",
                          error=str(_lock_del_err))
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
        lock_entity = lead_data.get("lock_entity")
        if lock_entity:
            try:
                _db().collection("global_lead_locks").document(lock_entity).delete()
            except Exception as _lock_del_err:
                log.error("lead_lock_delete_failed",
                          lock_entity=lock_entity,
                          url=url[:80] if url else "unknown",
                          error=str(_lock_del_err))
        return jsonify({"error": str(eval_err)}), 500

    score = evaluation.get("score", 0)
    is_thin = len(text.strip()) < 500
    threshold = 6 if is_thin else 7

    if score >= threshold:
        contact_endpoints = []
        for e in list(evaluation.get("contact_endpoints", [])):
            if e.get("platform") == "email" and _is_generic_email(e.get("uri", "")):
                continue
            contact_endpoints.append(e)
        existing_uris     = {e["uri"] for e in contact_endpoints}
        for em in (emails or [])[:3]:
            if em and em not in existing_uris and not _is_generic_email(em):
                contact_endpoints.append({"platform": "email", "uri": em})
                existing_uris.add(em)
        for ph in (phones or [])[:2]:
            if ph and ph not in existing_uris:
                contact_endpoints.append({"platform": "other", "uri": ph})
                existing_uris.add(ph)

        # V24.1.1 FIX: Write full lead payload matching _process_single_url().
        # Previously only wrote 4 fields — CRM displayed blank company_name,
        # decision_maker, intent_signal, etc. for scraper-heavy leads.
        lead_payload = {
            "score":                        score,
            "dm":                           evaluation.get("dm", ""),
            "pain_point":                   evaluation.get("pain_point", ""),
            "contact_endpoints":            contact_endpoints,
            "status":                       "new",
            "intent_signal":                evaluation.get("intent_signal", ""),
            "hiring_intent_found":          evaluation.get("hiring_intent_found", "No"),
            "tech_stack_found":             tech_stack,
            "icebreaker_angle":             evaluation.get("icebreaker_angle", ""),
            "decision_maker_name":          evaluation.get("decision_maker_name", "Unknown"),
            "decision_maker_title":         evaluation.get("decision_maker_title", "Unknown"),
            "company_size_tier":            evaluation.get("company_size_tier", "Unknown"),
            "primary_objection_hypothesis": evaluation.get("primary_objection_hypothesis", "Unknown"),
            "company_name":                 evaluation.get("company_name"),
            "matched_campaign_ids":         evaluation.get("matched_campaign_ids", []),
            "score_reasoning":              evaluation.get("score_reasoning", ""),
            "confidence_level":             evaluation.get("confidence_level", "SPECULATIVE"),
            "evidence_chain":               evaluation.get("evidence_chain", []),
            "origin_engine":                "scraper-heavy",
        }
        doc_ref.set(lead_payload, merge=True)
        _settle_credit(tenant_id, "success", lead_id=lead_id)
        log.info("finalize_lead_written", lead_id=lead_id[:24], score=score)
    else:
        doc_ref.delete()
        # V24.1.1 FIX: Settle credit on failure path too (was missing — accounting gap)
        _settle_credit(tenant_id, "failure", lead_id=lead_id)
        log.info("finalize_score_gate_drop", lead_id=lead_id[:24], score=score)
        lock_entity = lead_data.get("lock_entity")
        if lock_entity:
            try:
                _db().collection("global_lead_locks").document(lock_entity).delete()
            except Exception as _lock_del_err:
                log.error("lead_lock_delete_failed",
                          lock_entity=lock_entity,
                          url=url[:80] if url else "unknown",
                          error=str(_lock_del_err))

    return jsonify({"status": "ok", "score": score}), 200


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _shadow_track_bq(
    tenant_id: str,
    campaign_id: str,
    lead_id: str,
    url: str,
    score: int,
    sourcing_vector: str,
    pain_point: str,
    prism_mode: str,
) -> None:
    """Fire-and-forget BigQuery write for qualified dispatch leads.

    SF-013 FIX: The monolith's /produce route pushed shadow-track rows to
    BigQuery but /dispatch never did — the consumer's qualitative outcomes
    (score, pain_point, prism_mode) were never fed back into the swarm
    analytics dataset, starving the RLHF statistical router of the richest
    signal source (leads that actually passed the Gemini score gate).

    This function runs in a daemon thread so it never blocks the scraping loop.
    All exceptions are swallowed — BQ telemetry must never crash a lead write.

    Target table: lead-sniper-prod.swarm_analytics.shadow_track_events
    (one row per qualified dispatch lead).
    """
    def _write() -> None:
        try:
            import datetime as _dt
            from core.clients import get_bq_client  # type: ignore[import]
            from google.cloud import bigquery as _bq  # type: ignore[import]

            row = {
                "tenant_id":       tenant_id,
                "campaign_id":     campaign_id,
                "lead_id":         lead_id,
                "url":             url[:500],
                "score":           score,
                "sourcing_vector": sourcing_vector,
                "pain_point":      (pain_point or "")[:1000],
                "prism_mode":      prism_mode,
                "stage":           "dispatch",  # differentiates from produce-side rows
                "timestamp":       _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            errors = get_bq_client().insert_rows_json(
                "lead-sniper-prod.swarm_analytics.shadow_track_events",
                [row],
            )
            if errors:
                log.warning("shadow_track_bq_insert_errors",
                            errors=str(errors)[:200], lead_id=lead_id[:16])
            else:
                log.debug("shadow_track_bq_ok", lead_id=lead_id[:16], score=score)
        except Exception as _bq_err:
            # Non-critical — never surface to scraping loop
            log.warning("shadow_track_bq_failed", error=str(_bq_err)[:120])

    import threading as _th
    _th.Thread(target=_write, daemon=True).start()

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
                # V24.1.1 FIX: Attach OIDC token mirroring _settle_credit() pattern.
                # Without this, scraper-heavy rejects all deferred tasks with 401,
                # silently dropping leads that need JS rendering (SPAs, WAF-blocked).
                **({"oidc_token": {
                    "service_account_email": ORCHESTRATOR_SA_EMAIL,
                    "audience": SCRAPER_HEAVY_URL,
                }} if ORCHESTRATOR_SA_EMAIL else {}),
            }
        })
        log.info("dispatch_deferred_to_scraper_heavy", url=url[:80],
                 oidc_configured=bool(ORCHESTRATOR_SA_EMAIL))
    except Exception as defer_err:
        log.warning("dispatch_scraper_heavy_defer_failed",
                    url=url[:80], error=str(defer_err))


def _maybe_notify_whatsapp(tenant_id: str, url: str, lead_id: str,
                            score: int, evaluation: dict) -> None:
    """Fire WhatsApp Business API notification for score >= 8 leads."""
    try:
        import httpx as _httpx
        # V24.2 (L4-8): WhatsApp feature flag guard.
        # AGENTS.md: "WhatsApp features are disabled — do not re-enable without explicit approval."
        # Gated behind Firestore feature flag so the data-presence guard below is not
        # sufficient to re-enable the feature accidentally.
        try:
            _ff_doc = _db().collection("system_telemetry").document("feature_flags").get()
            _ff_data = _ff_doc.to_dict() or {} if _ff_doc.exists else {}
            if not _ff_data.get("whatsapp_enabled", False):
                return  # Feature disabled by policy
        except Exception as _ff_err:
            log.warning("whatsapp_feature_flag_read_failed", error=str(_ff_err),
                        fallback="Skipping WhatsApp notification (fail-safe).")
            return  # Fail-safe: skip on flag read failure
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
        resp = _httpx.post(
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
        resp.raise_for_status()
        resp_json = resp.json()
        wa_message_id = None
        if "messages" in resp_json and len(resp_json["messages"]) > 0:
            wa_message_id = resp_json["messages"][0].get("id")
        
        if wa_message_id:
            _db().collection("leads").document(lead_id).update({"wa_message_id": wa_message_id})
            log.info("whatsapp_notification_sent", tenant_id=tenant_id, score=score, wa_message_id=wa_message_id)
        else:
            log.info("whatsapp_notification_sent_no_id", tenant_id=tenant_id, score=score)
    except Exception as wa_err:
        log.warning("whatsapp_notification_failed", error=str(wa_err))


# ── V26.0 Phase 4: Entity Extraction Engine ──────────────────────────────────
# For PLATFORM_MINING and COMPETITOR_TOUCHPOINT strategies, a single aggregator
# page can yield 5-10 individual leads (agents, businesses, reviewers).
# This function replaces the standard text scoring with structured entity
# extraction from the DOM.
#
# Per-domain rate limiting: Track domain scrape counts in memory to prevent
# hammering a single platform. Max 5 pages per domain per dispatch batch.

_ENTITY_DOMAIN_COUNTS: dict[str, int] = {}  # Reset each dispatch batch
_ENTITY_DOMAIN_MAX = 5
_ENTITY_DOMAIN_LOCK = __import__('threading').Lock()  # V26.0.1: Thread safety for TPE

_ENTITY_EXTRACTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "entities": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {
                        "type": "STRING",
                        "description": "Full name of the person or business.",
                    },
                    "role_or_title": {
                        "type": "STRING",
                        "description": "Job title, professional role, or business category.",
                    },
                    "company": {
                        "type": "STRING",
                        "description": "Company or organization name. Use the entity name itself if it IS a company.",
                    },
                    "phone": {
                        "type": "STRING",
                        "description": "Phone number if visible on the page. Empty string if not found.",
                    },
                    "email": {
                        "type": "STRING",
                        "description": "Email address if visible on the page. Empty string if not found.",
                    },
                    "profile_url": {
                        "type": "STRING",
                        "description": "Direct URL to this entity's profile page if available.",
                    },
                    "relevance_score": {
                        "type": "INTEGER",
                        "description": "0-100 relevance score to the ICP context. 100 = perfect match.",
                    },
                    "extraction_note": {
                        "type": "STRING",
                        "description": "Brief note on why this entity is relevant.",
                    },
                },
                "required": ["name", "relevance_score"],
            },
            "description": "All individual entities (people, businesses, agents) found on this page.",
        },
        "page_type": {
            "type": "STRING",
            "description": "Classification of the source page: 'directory', 'review_page', 'listing', 'social_post', 'other'.",
        },
        "entity_count": {
            "type": "INTEGER",
            "description": "Total number of entities extracted.",
        },
    },
    "required": ["entities", "page_type", "entity_count"],
}


def _extract_entities_from_dom(
    text: str,
    icp_context: str,
    campaign: dict,
    source_url: str,
    tenant_id: str,
    campaign_id: str,
    lead_id: str,
) -> list[dict]:
    """Extract structured entities from an aggregator/review page DOM.

    For PLATFORM_MINING and COMPETITOR_TOUCHPOINT strategies.
    Each extracted entity becomes a separate lead in Firestore.

    Returns list of entity dicts with: name, role, company, phone, email,
    profile_url, relevance_score, extraction_note.

    Non-fatal: returns empty list on any failure.
    """

    # Per-domain rate limit (thread-safe for ThreadPoolExecutor)
    from urllib.parse import urlparse as _urlparse
    _domain = _urlparse(source_url).netloc.lower() if source_url else ""
    with _ENTITY_DOMAIN_LOCK:
        _domain_count = _ENTITY_DOMAIN_COUNTS.get(_domain, 0)
        if _domain_count >= _ENTITY_DOMAIN_MAX:
            log.info("entity_extraction_domain_rate_limited",
                     domain=_domain, count=_domain_count, max=_ENTITY_DOMAIN_MAX,
                     note="Skipping entity extraction to avoid platform rate-limiting.")
            return []
        _ENTITY_DOMAIN_COUNTS[_domain] = _domain_count + 1

    # Guard against very short text
    if len(text.strip()) < 200:
        log.info("entity_extraction_text_too_short",
                 text_chars=len(text), url=source_url[:80])
        return []

    strategy_info = campaign.get("intelligence_strategy", {})
    strategy = strategy_info.get("primary", "")
    platform_targets = strategy_info.get("platform_targets", [])
    competitor_names = strategy_info.get("competitor_names", [])

    try:
        from services.gemini_service import call_gemini_2_5  # type: ignore[import]

        prompt = (
            "You are an OSINT entity extractor. Analyze this webpage content and extract "
            "ALL individual people, businesses, agents, or companies listed on it.\n\n"
            f"TARGET ICP: {icp_context[:1500]}\n\n"
            f"STRATEGY: {strategy}\n"
        )

        if strategy == "PLATFORM_MINING":
            prompt += (
                f"\nPLATFORM TARGETS: {', '.join(platform_targets[:5])}\n"
                "This is a DIRECTORY/LISTING page. Extract every agent, vendor, or "
                "business profile visible. Include their contact info if shown.\n"
                "Each extracted entity is a potential lead to contact.\n"
            )
        elif strategy == "COMPETITOR_TOUCHPOINT":
            prompt += (
                f"\nCOMPETITOR NAMES: {', '.join(competitor_names[:5])}\n"
                "This is a REVIEW/ENGAGEMENT page. Extract:\n"
                "- Reviewers who left reviews (they may need the same service)\n"
                "- Businesses that responded to reviews (competitors to study)\n"
                "- People who commented or engaged publicly\n"
                "The REVIEWER is the lead — they have an active need.\n"
            )

        prompt += (
            f"\n\nWEBPAGE CONTENT (first 8000 chars):\n{text[:8000]}\n\n"
            "RULES:\n"
            "- Extract REAL entities only — no generic placeholders\n"
            "- Include contact info (phone, email, profile URL) when visible\n"
            "- Score each entity 0-100 based on ICP relevance\n"
            "- Skip entities that are clearly bots, spam, or irrelevant\n"
            "- Maximum 15 entities per page\n"
        )

        result = call_gemini_2_5(
            prompt,
            response_schema=_ENTITY_EXTRACTION_SCHEMA,
        )

        if not result or not isinstance(result, dict):
            log.warning("entity_extraction_empty_result", url=source_url[:80])
            return []

        entities = result.get("entities", [])
        page_type = result.get("page_type", "other")

        # Filter: only entities with relevance >= 30
        qualified = [
            e for e in entities
            if isinstance(e, dict) and e.get("relevance_score", 0) >= 30
        ]

        log.info("entity_extraction_complete",
                 url=source_url[:80],
                 raw_count=len(entities),
                 qualified_count=len(qualified),
                 page_type=page_type,
                 strategy=strategy)

        # Create leads for each qualified entity
        created_leads = []
        for idx, entity in enumerate(qualified[:10]):  # Cap at 10 per page
            entity_name = (entity.get("name") or "").strip()
            if not entity_name or len(entity_name) < 2:
                continue

            entity_lead_id = f"{lead_id}_entity_{idx}"
            # V26.0.1: Build contact_endpoints in standard list[dict] format
            _entity_contacts = []
            _entity_email = (entity.get("email") or "").strip()
            _entity_phone = (entity.get("phone") or "").strip()
            if _entity_email:
                _entity_contacts.append({"platform": "email", "uri": _entity_email})
            if _entity_phone:
                _entity_contacts.append({"platform": "phone", "uri": _entity_phone})

            entity_payload = {
                "id":                   entity_lead_id,
                "source_url":           entity.get("profile_url") or source_url,
                "tenant_id":            tenant_id,
                "campaign_id":          campaign_id,
                "origin_engine":        "entity_extractor",
                "score":                min(entity.get("relevance_score", 50), 100) // 10,
                "normalized_score":     min(entity.get("relevance_score", 50), 100),
                "matched_campaign_ids": [campaign_id],
                "matched_campaigns":    [campaign_id],
                "company_name":         entity.get("company") or entity_name,
                "decision_maker_name":  entity_name,
                "decision_maker_title": entity.get("role_or_title", "Unknown"),
                "pain_point":           entity.get("extraction_note", ""),
                "dm":                   "",
                "intent_signal":        f"Extracted from {page_type} page: {source_url[:60]}",
                "contact_endpoints":    _entity_contacts,
                "signal_source_type":   "platform_entity",
                "signal_platform":      _domain,
                "source_type":          "platform_entity",
                "status":               "new",
                "is_in_crm":            False,
                "sourcing_vector":      campaign.get("sourcing_vector", "B2B"),
                "prism_mode":           "entity_extraction",
                "is_cluster_lead":      False,
                "cluster_id":           "",
                "convergence_score":    0.0,
                "cluster_signals":      [],
                "cluster_snippets":     [],
                "cluster_platforms":    [],
                "cluster_summary":      "",
                "source_diversity":     0,
                "cluster_label":        "",
                "createdAt":            firestore.SERVER_TIMESTAMP,
                "updatedAt":            firestore.SERVER_TIMESTAMP,
            }

            try:
                _db().collection("leads").document(entity_lead_id).set(
                    entity_payload, merge=True
                )
                created_leads.append(entity_payload)
                log.info("entity_lead_created",
                         entity_name=entity_name[:50],
                         relevance=entity.get("relevance_score", 0),
                         lead_id=entity_lead_id,
                         campaign_id=campaign_id)
            except Exception as _entity_write_err:
                log.warning("entity_lead_write_failed",
                            entity_name=entity_name[:50],
                            error=str(_entity_write_err))

        return created_leads

    except Exception as _extract_err:
        log.warning("entity_extraction_failed",
                    url=source_url[:80],
                    error=str(_extract_err))
        return []
