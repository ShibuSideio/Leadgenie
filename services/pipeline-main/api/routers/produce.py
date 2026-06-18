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

    log.info(
        "TRACE-5: Campaign fetched.",
        sourcing_vector=campaign.get("sourcing_vector"),
    )

    sourcing_vector = campaign.get("sourcing_vector", "Classic B2B")
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

    # Synthesise keywords from bio if empty
    if not keywords:
        if bio:
            keywords = [w.strip() for w in bio.split() if len(w.strip()) > 3][:5]
            log.info("keywords_synthesised_from_bio",
                     count=len(keywords), campaign_id=campaign_id)
        if not keywords:
            log.critical(
                "produce_empty_keywords",
                campaign_id=campaign_id,
                persona_id=_persona_id,
                persona_keywords=campaign.get("persona_keywords"),
                keywords=campaign.get("keywords"),
                bio=campaign.get("bio"),
                note="ABORT: No Serper query can be constructed.",
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

        raw_results = search_serper(
            search_query,
            location=clean_location or None,
            gl=gl or None,
            campaign_id=campaign_id,
            tenant_id=tenant_id,
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
                }

    fetched_count = len(raw_urls)
    log.info("TRACE-10: Serper loop complete.", fetched_count=fetched_count)

    # ------------------------------------------------------------------
    # Snippet cache: persist snippets universally for two-stage funnel
    # ------------------------------------------------------------------
    for surl, meta in snippet_db.items():
        s_domain    = extract_root_domain(surl)
        is_social   = any(s_domain.endswith(d) for d in _SOCIAL_DOMAINS_PRODUCER)
        
        # Calculate matching dedup key to align scraped_cache document ID with dispatch lead_id
        if is_social:
            from urllib.parse import urlparse as _urlparse
            parsed = _urlparse(surl)
            dedup_key = f"{parsed.netloc}{parsed.path}".lower().replace("www.", "")
        else:
            dedup_key = s_domain
            
        cache_key = hashlib.sha256(f"{tenant_id}_{dedup_key}".encode()).hexdigest()
        combined  = f"{meta['title']}\n{meta['snippet']}".strip()
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
                d_is_social = any(
                    extract_root_domain(u).endswith(s)
                    for s in _SOCIAL_DOMAINS_PRODUCER
                )
                dedup_key = u if d_is_social else extract_root_domain(u)
                existing_ids.add(
                    hashlib.sha256(f"{tenant_id}_{dedup_key}".encode()).hexdigest()
                )
                existing_ids.add(u)
    except Exception as exc:
        log.warning("produce_dedup_query_failed", error=str(exc))

    fresh_urls: list[str] = []
    for url in raw_urls:
        f_is_social = any(
            extract_root_domain(url).endswith(s)
            for s in _SOCIAL_DOMAINS_PRODUCER
        )
        dedup_key = url if f_is_social else extract_root_domain(url)
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
    combined_queue = list(dict.fromkeys(current_queue + fresh_urls))[:200]

    try:
        update_data = {
            "unprocessed_queue": combined_queue,
            "last_produced_at":  firestore.SERVER_TIMESTAMP,
        }
        if not current_queue and combined_queue:
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

    log.info("TRACE-DONE: produce() complete.",
             campaign_id=campaign_id, queue_depth=len(combined_queue))

    return jsonify({
        "status":        "produced",
        "fetched":       fetched_count,
        "deduplicated":  duped_count,
        "queued":        queued_count,
        "queue_depth":   len(combined_queue),
    }), 200
