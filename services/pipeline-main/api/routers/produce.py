"""
Pipeline-Main V23 — /produce Blueprint (FULL IMPLEMENTATION).

THE PRODUCER — 24-Hour Serper Fetch Job.
=========================================
Runs Intent Translation (Query Brain) + Serper Execution.
Deduplicates against global leads collection.
Writes fresh URLs to campaigns/{id}.unprocessed_queue.
Does NOT call the Gemini Gate — only the Consumer does.

Raw GCS firehose dump deliberately removed per EA directive (2026-04-18).
Intelligence is sourced exclusively from BigQuery swarm_analytics via
the shadow_track hook — no parallel GCS write path exists.

Auth:
  - Zero-Trust OIDC: Google-signed JWT verified by @require_tasks_oidc.
  - Defense-in-depth: X-CloudTasks-QueueName header also enforced.
  - Cloud Run IAM (--no-allow-unauthenticated) is the outermost gate.
"""
from __future__ import annotations

import hashlib

from google.cloud import firestore  # type: ignore[import]
from google.cloud.firestore_v1.base_query import FieldFilter  # type: ignore[import]
from flask import Blueprint, jsonify, request

from core.logging import get_logger    # type: ignore[import]
from core.clients import get_db        # type: ignore[import]
from middleware.oidc import require_tasks_oidc  # type: ignore[import]
from services.query_brain import generate_smart_query  # type: ignore[import]
from services.query_brain import _is_consumer_archetype  # type: ignore[import]
from services.serper_service import (  # type: ignore[import]
    search_serper,
    filter_serper_noise,
    extract_root_domain,
    SOCIAL_DOMAINS,
)
from services.telemetry import update_circuit_telemetry  # type: ignore[import]

bp  = Blueprint("produce", __name__)
log = get_logger("pipeline.produce")

_SOCIAL_DOMAINS_PRODUCER = SOCIAL_DOMAINS

# ---------------------------------------------------------------------------
# FIX (2026-06-21): System error string ingestion filter.
# Firestore campaign documents occasionally contain error messages, fallback
# sentinels, or log fragments that were accidentally persisted as keyword or
# bio values. When ingested, these produce searches like:
#   "fallback intent processing required" -wiki -jobs ...
# which return zero useful results and waste Serper credits.
# ---------------------------------------------------------------------------
_SYSTEM_JUNK_PATTERNS: frozenset[str] = frozenset({
    "fallback intent processing required",
    "error",
    "exception",
    "traceback",
    "internal server error",
    "timeout",
    "failed to",
    "null",
    "undefined",
    "none",
    "n/a",
    "child_campaign_override",
    "shadow_learner",
    "[shadow_learner",
    "placeholder",
    "test_keyword",
    "sample_data",
})


@bp.route("/produce", methods=["POST"])
@require_tasks_oidc
def produce():
    """V23 Producer — Intent Translation + Serper Execution.

    TRACE log convention (matches Cloud Run log filter):
      ``jsonPayload.message =~ "TRACE-[0-9]+"``
    """
    # ------------------------------------------------------------------
    # TRACE-1: Payload parsing
    # ------------------------------------------------------------------
    log.info("TRACE-1: produce() entered. Parsing payload.", path=request.path)
    lead_data   = request.json or {}
    tenant_id   = lead_data.get("tenant_id")
    campaign_id = lead_data.get("campaign_id")
    log.info("TRACE-2: payload parsed.", tenant_id=tenant_id, campaign_id=campaign_id)

    if not tenant_id or not campaign_id:
        log.critical(
            "produce_missing_ids",
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            note="ABORT: Cloud Task payload must include tenant_id and campaign_id.",
        )
        return jsonify({"error": "Missing campaign_id or tenant_id"}), 400

    # ------------------------------------------------------------------
    # TRACE-3/4: Campaign document fetch
    # ------------------------------------------------------------------
    log.info("TRACE-3: Acquiring Firestore handle (lazy init).")
    campaign_ref = get_db().collection("campaigns").document(campaign_id)
    log.info("TRACE-4: Firestore handle ready. Fetching campaign document.")

    try:
        campaign = campaign_ref.get().to_dict() or {}
    except Exception as exc:
        log.critical(
            "produce_campaign_fetch_failed",
            campaign_id=campaign_id,
            error=str(exc),
            exc_info=True,
        )
        return jsonify({"error": "Firestore error fetching campaign"}), 500

    if campaign.get("tenant_id") != tenant_id:
        log.warning("produce_unauthorized_tenant_context", campaign_id=campaign_id, tenant_id=tenant_id)
        return jsonify({"error": "Unauthorized tenant context"}), 403

    log.info(
        "TRACE-5: Campaign fetched.",
        sourcing_vector=campaign.get("sourcing_vector"),
    )

    sourcing_vector = campaign.get("sourcing_vector", "B2B")
    location        = campaign.get("location", "").strip()
    gl              = campaign.get("gl", "").strip()

    # ------------------------------------------------------------------
    # Persona Vault field extraction (V23 Persona Vault precedence fix)
    # ------------------------------------------------------------------
    _persona_id   = campaign.get("persona_id", "")
    _persona_bio  = campaign.get("persona_bio", "").strip()
    _persona_keys = campaign.get("persona_keywords", "").strip()

    bio = _persona_bio or campaign.get("bio", "")
    if _persona_id and _persona_bio:
        log.info(
            "persona_injected",
            persona_name=campaign.get("persona_name", _persona_id),
            bio_preview=bio[:60],
            campaign_id=campaign_id,
        )

    raw_keywords = _persona_keys or campaign.get("keywords", "")
    if isinstance(raw_keywords, str):
        keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]
    else:
        keywords = list(raw_keywords) if raw_keywords else []

    # CHILD_CAMPAIGN_OVERRIDE sentinel guard
    if bio == "CHILD_CAMPAIGN_OVERRIDE":
        bio = (
            campaign.get("effective_bio")
            or campaign.get("campaign_focus")
            or ", ".join(keywords)
        )
        log.info("child_campaign_override_resolved", bio_preview=bio[:80])

    # V25.3.1: Preserve raw bio BEFORE enrichment for keyword synthesis.
    # build_enriched_context() adds structural labels ("PRODUCT/SERVICE:",
    # "BUYER TYPE:") that must NOT leak into Serper search queries.
    _raw_bio = (campaign.get("bio") or campaign.get("effective_bio") or
                campaign.get("persona_bio") or campaign.get("name") or "").strip()

    # V24.6.1: Replace thin bio assembly with build_enriched_context().
    # Previously: picked ONE field (persona_bio OR bio) and ignored all others.
    # Now: aggregates ALL 15+ campaign fields (effective_bio, pain_point,
    # target_angle_hook, unfair_advantage, persona_name, geo_hierarchy, etc.)
    # into a structured ICP context. Handles sparse campaigns (user filled only
    # campaign name + location) and rich campaigns (all fields filled) equally.
    # Overrides the above `bio` variable entirely.
    try:
        from services.context_builder import build_enriched_context  # type: ignore[import]
        bio = build_enriched_context(campaign)
    except Exception as _ctx_err:
        log.warning(
            "context_builder_failed",
            campaign_id=campaign_id,
            error=str(_ctx_err),
            note="Falling back to raw bio field. Check context_builder.py.",
        )
        # bio stays as-is from the persona vault logic above

    # ------------------------------------------------------------------
    # FIX (2026-06-21): Bio field sanitizer.
    # Scrub the bio if it contains system error strings or sentinels
    # that should never reach the Gemini prompt (they cause intent
    # hallucination and system-error-string searches).
    # Uses a stricter set than keywords — generic words like "error"
    # could appear legitimately in a campaign bio.
    # ------------------------------------------------------------------
    _BIO_JUNK_PATTERNS: set[str] = {
        "fallback intent processing required",
        "internal server error",
        "traceback",
        "child_campaign_override",
        "shadow_learner",
        "[shadow_learner",
        "test_keyword",
        "sample_data",
        "placeholder bio",
        "undefined",
    }
    if bio and any(junk in bio.lower() for junk in _BIO_JUNK_PATTERNS):
        log.warning(
            "produce_bio_sanitized",
            campaign_id=campaign_id,
            original_bio_preview=bio[:120],
            note="Bio field contains system junk. Cleared to prevent prompt pollution.",
        )
        bio = ""

    # Synthesise keywords from bio if empty
    if not keywords:
        if _raw_bio:
            # V25.3.1: Use raw bio, not enriched context, to prevent
            # structural labels from becoming Serper search terms.
            keywords = [w.strip() for w in _raw_bio.split() if len(w.strip()) > 3][:5]
            log.info("keywords_synthesised_from_bio",
                     count=len(keywords), campaign_id=campaign_id,
                     source="raw_bio")

    # ------------------------------------------------------------------
    # FIX (2026-06-21): Keyword ingestion sanitizer.
    # Drop any keywords that match known system error strings, log
    # fragments, or fallback sentinels before they reach Query Brain.
    # ------------------------------------------------------------------
    _raw_count = len(keywords)
    keywords = [
        kw for kw in keywords
        if kw.strip()
        and len(kw.strip()) > 2
        and not any(junk in kw.lower() for junk in _SYSTEM_JUNK_PATTERNS)
    ]
    _dropped = _raw_count - len(keywords)
    if _dropped > 0:
        log.warning(
            "produce_keywords_sanitized",
            campaign_id=campaign_id,
            dropped=_dropped,
            remaining=len(keywords),
            note="System error strings or sentinel values removed from keywords.",
        )

    if not keywords:
        log.critical(
            "produce_empty_keywords",
            campaign_id=campaign_id,
            persona_id=_persona_id,
            persona_keywords=campaign.get("persona_keywords"),
            keywords=campaign.get("keywords"),
            bio=campaign.get("bio"),
            note="ABORT: No Serper query can be constructed (post-sanitization).",
        )
        return jsonify({
            "error":       "Empty keywords matrix",
            "campaign_id": campaign_id,
            "debug": {
                "persona_id":        _persona_id,
                "persona_keywords":  campaign.get("persona_keywords"),
                "keywords":          campaign.get("keywords"),
                "bio":               campaign.get("bio"),
            },
        }), 400

    # ------------------------------------------------------------------
    # FIX (2026-06-21): Location field validation guard.
    # Reject location values that are obviously not geographic (audience
    # descriptions, error messages, or strings > 100 chars).
    # ------------------------------------------------------------------
    _LOCATION_JUNK_TOKENS = {
        "interested", "customers", "vehicle", "users", "audience",
        "persona", "error", "exception", "fallback", "null",
    }
    if location and (
        len(location) > 100
        or any(tok in location.lower() for tok in _LOCATION_JUNK_TOKENS)
    ):
        log.warning(
            "produce_location_rejected",
            campaign_id=campaign_id,
            original_location=location[:120],
            note="Location field contains non-geographic data. Reset to empty.",
        )
        location = ""

    log.info(
        "TRACE-6: Keywords resolved.",
        keyword_count=len(keywords),
        bio_len=len(bio),
        sourcing_vector=sourcing_vector,
    )

    # Persona negative targeting signals ("NOT <phrase>" → Serper exclusion operators)
    _targeting_signals: list[str] = campaign.get("persona_targeting_signals") or []
    if _targeting_signals:
        neg_count = sum(1 for s in _targeting_signals if s.upper().startswith("NOT "))
        log.info(
            "persona_targeting_signals_loaded",
            total=len(_targeting_signals),
            negative=neg_count,
            campaign_id=campaign_id,
        )

    # ------------------------------------------------------------------
    # TRACE-7: Query Brain (Intent Translation)
    # ------------------------------------------------------------------
    log.info("TRACE-7: Calling generate_smart_query() (Vertex AI).")
    _persona_cat = (
        campaign.get("persona_name") or campaign.get("name") or "general"
    ).strip()

    try:
        smart_keywords = generate_smart_query(
            keywords, tenant_id, bio, sourcing_vector,
            persona_category=_persona_cat,
            targeting_signals=_targeting_signals,
            campaign_id=campaign_id,
        )
    except Exception as exc:
        log.critical(
            "produce_query_brain_failed",
            campaign_id=campaign_id,
            error=str(exc),
            exc_info=True,
        )
        return jsonify({"error": "Query Brain failed", "details": str(exc)}), 500

    log.info("TRACE-8: generate_smart_query() complete.",
             smart_keyword_count=len(smart_keywords))

    # ------------------------------------------------------------------
    # FIX (2026-06-21): Post-generation query sanitizer.
    # Drop any generated Serper queries that contain system error strings
    # or internal pipeline terms that should never reach Serper.
    # ------------------------------------------------------------------
    _pre_sanitize_count = len(smart_keywords)
    smart_keywords = [
        sq for sq in smart_keywords
        if not any(junk in sq.lower() for junk in _SYSTEM_JUNK_PATTERNS)
    ]
    _sq_dropped = _pre_sanitize_count - len(smart_keywords)
    if _sq_dropped > 0:
        log.warning(
            "produce_smart_queries_sanitized",
            campaign_id=campaign_id,
            dropped=_sq_dropped,
            remaining=len(smart_keywords),
            note="System junk detected in generated Serper queries. Dropped.",
        )

    if not smart_keywords:
        log.warning(
            "produce_all_queries_sanitized_empty",
            campaign_id=campaign_id,
            note="All generated queries were system junk. Nothing to search.",
        )
        return jsonify({
            "status": "produced",
            "fetched": 0,
            "deduplicated": 0,
            "queued": 0,
            "queue_depth": len(campaign.get("unprocessed_queue", [])),
            "warning": "All queries sanitized as system junk.",
        }), 200

    # Telemetry: bill the expected Serper calls
    try:
        get_db().collection("usage_metrics").document(tenant_id).set(
            {"serper_searches": firestore.Increment(len(smart_keywords))}, merge=True
        )
    except Exception:
        pass  # non-fatal

    # ------------------------------------------------------------------
    # TRACE-9: Serper Execution loop
    # ------------------------------------------------------------------
    raw_urls:   list[str] = []
    snippet_db: dict[str, dict] = {}

    log.info("TRACE-9: Entering Serper execution loop.",
             keyword_count=len(smart_keywords))

    for kw in smart_keywords:
        clean_location = location if location and location.lower() != "all" else ""
        search_query   = kw

        # V25.3.0: Split Serper strategy by sourcing vector.
        # B2B niche queries (boolean dorks with buyer-language phrases) return
        # 0 results on geo-restricted Google indexes (gl=in, gl=ae, etc.).
        # The old dual-query pattern sent the geo call first then retried
        # globally — doubling Serper credit spend with zero benefit for B2B.
        # Consumer archetypes (B2C, D2C, B2B2C) still benefit from geo-
        # restricted indexes because local business discovery depends on
        # Google's locale-specific ranking.
        _is_consumer_vector = _is_consumer_archetype(sourcing_vector)

        if _is_consumer_vector:
            # Consumer: geo-restricted first, then global fallback
            raw_results = search_serper(
                search_query,
                location=clean_location or None,
                gl=gl or None,
                campaign_id=campaign_id,
                tenant_id=tenant_id,
                sourcing_vector=sourcing_vector,
            )
            if not raw_results and gl:
                log.info(
                    "produce_geo_fallback",
                    query=search_query[:80],
                    original_gl=gl,
                    sourcing_vector=sourcing_vector,
                    note="Consumer geo-restricted call returned 0 results. "
                         "Retrying on global index.",
                    campaign_id=campaign_id,
                )
                raw_results = search_serper(
                    search_query,
                    location=None,
                    gl=None,
                    campaign_id=campaign_id,
                    tenant_id=tenant_id,
                    sourcing_vector=sourcing_vector,
                )
        else:
            # B2B: global-only (geo terms already in query text from query_brain).
            # V25.3.0: B2B niche queries return 0 results on geo-restricted
            # indexes. Geo relevance handled by Gemini scoring downstream.
            raw_results = search_serper(
                search_query,
                location=None,
                gl=None,
                campaign_id=campaign_id,
                tenant_id=tenant_id,
                sourcing_vector=sourcing_vector,
            )

        update_circuit_telemetry("serper_call")

        filtered = filter_serper_noise(raw_results)
        for r in filtered:
            link = r.get("link")
            if link and link not in raw_urls:
                raw_urls.append(link)
                snippet_db[link] = {
                    "title":   r.get("title", ""),
                    "snippet": r.get("snippet", ""),
                    "query":   search_query,
                }

    fetched_count = len(raw_urls)
    log.info("TRACE-10: Serper loop complete.", fetched_count=fetched_count)

    # ------------------------------------------------------------------
    # Snippet cache: persist snippets universally for two-stage funnel
    # ------------------------------------------------------------------
    # V24.5.4 FIX: Added buyer-forum platforms to shared_platforms.
    # Without this, B2B campaigns deduplicate reddit.com to ONE slot — meaning
    # 19 out of 20 Reddit buyer pain posts are silently dropped as domain-level
    # duplicates. Each Reddit/Quora/HN thread is a UNIQUE lead, not a domain.
    shared_platforms = {
        "linkedin.com", "medium.com", "substack.com", "wordpress.com", "github.io",
        # Buyer forum platforms — each thread/post is a unique lead (URL-path dedup)
        "reddit.com", "quora.com", "stackexchange.com", "stackoverflow.com",
        "news.ycombinator.com",   # Hacker News
        "community.hubspot.com", "community.g2.com",  # vendor community boards
        "forum.growthackers.com", "indiehackers.com",
    }
    for surl, meta in snippet_db.items():
        s_domain    = extract_root_domain(surl)
        is_social   = any(s_domain.endswith(d) for d in _SOCIAL_DOMAINS_PRODUCER)
        is_shared   = any(s_domain.endswith(d) for d in shared_platforms)
        
        # Calculate matching dedup key to align scraped_cache document ID with dispatch lead_id
        # P3 FIX (2026-06-20): B2C/Real Estate campaigns use URL-path dedup.
        # Domain-level dedup exhausts inventory after ~3 produce cycles because
        # listing aggregators (propertyfinder.ae, bayut.com) host thousands of
        # distinct listings under a single root domain.
        _is_b2c = _is_consumer_archetype(sourcing_vector)
        if is_social or is_shared or _is_b2c:
            from urllib.parse import urlparse as _urlparse
            parsed = _urlparse(surl)
            dedup_key = f"{parsed.netloc}{parsed.path}".lower().replace("www.", "")
        else:
            dedup_key = s_domain
            
        cache_key = hashlib.sha256(f"{tenant_id}_{dedup_key}".encode()).hexdigest()
        combined  = f"Query: {meta.get('query', '')}\nTitle: {meta['title']}\nSnippet: {meta['snippet']}".strip()
        if combined:
            try:
                get_db().collection("scraped_cache").document(cache_key).set({
                    "url":        surl,
                    "text":       combined,
                    "source":     "serper_snippet",
                    "tech_stack": [],
                    "emails":     [],
                    "phones":     [],
                }, merge=True)
            except Exception as exc:
                log.warning("snippet_persist_failed", url=surl, error=str(exc))

    # ------------------------------------------------------------------
    # Social-aware global deduplication
    # ------------------------------------------------------------------
    existing_ids: set[str] = set()
    try:
        # SF-005 FIX: Added .limit(500) to prevent full leads collection scan.
        # For tenants with >500 leads, only the 500 most recently indexed URLs
        # are checked. Fresh URLs beyond the 500-doc window may be re-queued.
        # Acceptable trade-off: occasional re-scrape of an old URL is far safer
        # than a minutes-long Firestore scan that blocks the producer worker.
        # TODO(SF-005): Implement cursor-based pagination when tenant leads > 5000.
        _DEDUP_SCAN_LIMIT = 500
        known_docs = list(
            get_db().collection("leads")
            .where(filter=FieldFilter("tenant_id", "==", tenant_id))
            .select(["url"])
            .limit(_DEDUP_SCAN_LIMIT)
            .stream()
        )
        if len(known_docs) == _DEDUP_SCAN_LIMIT:
            log.warning("produce_dedup_scan_cap_hit",
                        tenant_id=tenant_id,
                        limit=_DEDUP_SCAN_LIMIT,
                        note="Dedup scan capped. Tenant may have >500 leads. "
                             "Implement cursor pagination (SF-005) to prevent re-scrape.")
        for doc in known_docs:
            u = (doc.to_dict() or {}).get("url", "")
            if u:
                d_domain = extract_root_domain(u)
                d_is_social = any(
                    d_domain.endswith(s)
                    for s in _SOCIAL_DOMAINS_PRODUCER
                )
                d_is_shared = any(
                    d_domain.endswith(s)
                    for s in shared_platforms
                )
                # P3 FIX: B2C campaigns use URL-path dedup (matches snippet cache + fresh dedup)
                _is_b2c = _is_consumer_archetype(sourcing_vector)
                if d_is_social or d_is_shared or _is_b2c:
                    from urllib.parse import urlparse as _urlparse
                    parsed = _urlparse(u)
                    dedup_key = f"{parsed.netloc}{parsed.path}".lower().replace("www.", "")
                else:
                    dedup_key = d_domain
                existing_ids.add(
                    hashlib.sha256(f"{tenant_id}_{dedup_key}".encode()).hexdigest()
                )
                existing_ids.add(u)
    except Exception as exc:
        log.warning("produce_dedup_query_failed", error=str(exc))

    fresh_urls: list[str] = []
    for url in raw_urls:
        f_domain = extract_root_domain(url)
        f_is_social = any(
            f_domain.endswith(s)
            for s in _SOCIAL_DOMAINS_PRODUCER
        )
        f_is_shared = any(
            f_domain.endswith(d)
            for d in shared_platforms
        )
        # P3 FIX: B2C campaigns use URL-path dedup (matches snippet cache + existing leads)
        _is_b2c = _is_consumer_archetype(sourcing_vector)
        if f_is_social or f_is_shared or _is_b2c:
            from urllib.parse import urlparse as _urlparse
            parsed = _urlparse(url)
            dedup_key = f"{parsed.netloc}{parsed.path}".lower().replace("www.", "")
        else:
            dedup_key = f_domain
        lead_hash = hashlib.sha256(f"{tenant_id}_{dedup_key}".encode()).hexdigest()
        if lead_hash not in existing_ids and url not in existing_ids:
            fresh_urls.append(url)

    duped_count  = fetched_count - len(fresh_urls)
    queued_count = len(fresh_urls)
    log.info(
        "produce_dedup_complete",
        campaign_id=campaign_id,
        fetched=fetched_count,
        deduplicated=duped_count,
        queued=queued_count,
    )

    # ------------------------------------------------------------------
    # Write to unprocessed_queue (additive merge, cap at 200)
    # ------------------------------------------------------------------
    current_queue = campaign.get("unprocessed_queue", [])

    # V24.4 (L3-4): Queue backpressure — if queue depth > 150 unconsumed URLs,
    # skip producing new URLs. The consumer hasn't caught up yet. Producing more
    # would cause [:200] trimming to silently discard fresh signals.
    _queue_depth = len(current_queue) if current_queue else 0
    if _queue_depth > 150:
        log.info(
            "produce_skipped_queue_full",
            campaign_id=campaign_id,
            queue_depth=_queue_depth,
            threshold=150,
            note="Queue saturated. Skipping produce run — consumer must drain queue first. "
                 "Reduce drip_interval_minutes or increase dispatch frequency.",
        )
        return jsonify({"status": "skipped_queue_full", "queue_depth": _queue_depth}), 200

    combined_queue = list(dict.fromkeys(current_queue + fresh_urls))[:200]

    try:
        update_data = {
            "unprocessed_queue": combined_queue,
            "last_produced_at":  firestore.SERVER_TIMESTAMP,
        }
        # V24.4 (L3-5): Always update next_drip_due when the queue is refreshed,
        # not only on first fill. A stale next_drip_due causes immediate dispatch
        # on every sweep instead of respecting the configured drip cadence.
        if combined_queue:
            import datetime
            update_data["next_drip_due"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        campaign_ref.update(update_data)
    except Exception as exc:
        log.critical(
            "produce_queue_write_failed",
            campaign_id=campaign_id,
            error=str(exc),
            exc_info=True,
        )
        return jsonify({"error": "Queue write failed", "details": str(exc)}), 500

    # ------------------------------------------------------------------
    # V25.1.0: Signal Harvest — multi-source intent discovery pathway.
    #
    # Runs AFTER the Serper queue write so it cannot block the primary
    # produce flow. Uses a daemon thread with a 180s wall-clock budget.
    # The harvest writes directly to scraped_cache and unprocessed_queue
    # (same collections as the Serper pathway) — dispatch processes both
    # without any changes.
    #
    # Harvest is skipped when:
    #   - Queue is already saturated (handled above by backpressure gate)
    #   - HARVEST_ENABLED env var is "false" (opt-out for debugging)
    # ------------------------------------------------------------------
    import os as _os
    import threading as _threading

    harvest_metrics: dict = {}
    _harvest_enabled = _os.environ.get("HARVEST_ENABLED", "true").lower() != "false"

    if _harvest_enabled:
        # Extract Serper key for signal_harvest's SerperDiscoverySource
        # (same key already used by the Serper pathway above)
        _serper_key_for_harvest = ""
        try:
            from core.clients import get_serper_key  # type: ignore[import]
            _serper_key_for_harvest = get_serper_key() or ""
        except Exception:
            pass  # SerperDiscoverySource will be skipped without a key

        # Add campaign id to dict for signal_harvest (it's read from dict)
        _campaign_with_id = {**campaign, "id": campaign_id, "tenant_id": tenant_id}

        harvest_result_holder: list[dict] = []

        def _run_harvest() -> None:
            try:
                from services.signal_harvest import harvest_signals  # type: ignore[import]
                result = harvest_signals(
                    campaign      = _campaign_with_id,
                    db            = get_db(),
                    serper_api_key= _serper_key_for_harvest,
                )
                harvest_result_holder.append(result)
            except Exception as _h_exc:
                log.warning(
                    "signal_harvest_thread_failed",
                    campaign_id=campaign_id,
                    error=str(_h_exc),
                )

        harvest_thread = _threading.Thread(target=_run_harvest, daemon=True)
        harvest_thread.start()
        # 5-minute wall-clock budget: Google Reviews (5 competitors × 10 reviews
        # each) + PRISM enrichment + Gemini inline scoring can exceed 3 minutes.
        # 300s accommodates worst-case Serper + Gemini latency chains.
        harvest_thread.join(timeout=300)

        if harvest_result_holder:
            harvest_metrics = harvest_result_holder[0]
            log.info(
                "signal_harvest_pathway_complete",
                campaign_id=campaign_id,
                **harvest_metrics,
            )
        elif harvest_thread.is_alive():
            log.warning(
                "signal_harvest_thread_timeout",
                campaign_id=campaign_id,
                note="Harvest thread exceeded 300s budget. Results discarded.",
            )

    log.info("TRACE-DONE: produce() complete.",
             campaign_id=campaign_id, queue_depth=len(combined_queue))

    return jsonify({
        "status":        "produced",
        "fetched":       fetched_count,
        "deduplicated":  duped_count,
        "queued":        queued_count,
        "queue_depth":   len(combined_queue),
        # V25.1.0: Signal harvest pathway metrics
        "harvest": harvest_metrics,
    }), 200
