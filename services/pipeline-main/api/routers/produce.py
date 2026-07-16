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
import os
import re
from datetime import datetime, timezone, timedelta

from google.cloud import firestore  # type: ignore[import]
from google.cloud.firestore_v1.base_query import FieldFilter  # type: ignore[import]
from flask import Blueprint, jsonify, request

from core.logging import get_logger    # type: ignore[import]
from core.clients import get_db        # type: ignore[import]
from middleware.oidc import require_tasks_oidc  # type: ignore[import]


# ---------------------------------------------------------------------------
# V25.5.0: Content age filter — reject stale Reddit/forum posts
# ---------------------------------------------------------------------------
# Reddit URL slugs encode post date via the base36 ID. However, Serper results
# include a `date` field (ISO-8601 or relative like "2 months ago") that we can
# parse. For non-Reddit forums, we check snippet text for date indicators.

_STALE_DAYS_B2C = 90    # V26.0.5: 90 days (was 14). Agent profiles, property listings,
                        # and competitor directory pages are valid leads for months.
                        # The old 14-day window + qdr:m at Serper level was double-filtering,
                        # killing evergreen pages like dreoman.com/agent/mohammed.
_STALE_DAYS_B2B = 60    # B2B: 2 months — business discussions stay relevant longer


def _is_stale_content(result: dict, is_consumer: bool) -> bool:
    """Return True if Serper result is too old to be actionable.

    Checks the Serper ``date`` field (if present) against age thresholds.
    Falls back to title/snippet heuristics for date indicators.
    Returns False (not stale) if date cannot be determined — fail-open.
    """
    max_days = _STALE_DAYS_B2C if is_consumer else _STALE_DAYS_B2B
    raw_date = (result.get("date") or "").strip()
    if not raw_date:
        return False  # No date info — fail-open, let scoring decide

    # Try relative date parsing ("3 days ago", "2 months ago", "1 year ago")
    _rel_match = re.match(
        r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago",
        raw_date, re.IGNORECASE,
    )
    if _rel_match:
        _count = int(_rel_match.group(1))
        _unit = _rel_match.group(2).lower()
        _multipliers = {
            "second": 0, "minute": 0, "hour": 0,
            "day": 1, "week": 7, "month": 30, "year": 365,
        }
        age_days = _count * _multipliers.get(_unit, 0)
        return age_days > max_days

    # Try ISO-8601 date parsing ("2026-01-15T..." or "Jan 15, 2026")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            parsed = datetime.strptime(raw_date[:20], fmt)
            age = (datetime.now(timezone.utc) - parsed.replace(tzinfo=timezone.utc)).days
            return age > max_days
        except (ValueError, TypeError):
            continue

    return False  # Unparseable — fail-open
from services.query_brain import generate_smart_query  # type: ignore[import]
from services.query_brain import _is_consumer_archetype  # type: ignore[import]
from services.query_governance import (  # type: ignore[import]
    govern_query_portfolio,
    filter_queries_against_memory,
    build_exhaustion_escalation_queries,
    query_signature,
)
from services.domain_intelligence import (  # type: ignore[import]
    apply_domain_query_profile,
    build_domain_impact_summary,
    resolve_campaign_domain_profile,
)
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


def _is_recent_for_dedup(raw_created_at: object, cutoff: datetime) -> bool:
    if raw_created_at is None:
        return True
    if isinstance(raw_created_at, datetime):
        value = raw_created_at if raw_created_at.tzinfo else raw_created_at.replace(tzinfo=timezone.utc)
        return value >= cutoff
    if isinstance(raw_created_at, str):
        text = raw_created_at.strip()
        if not text:
            return True
        try:
            if text.endswith("Z"):
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            else:
                parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed >= cutoff
        except ValueError:
            return True
    return True


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
    # Domain profile: manual domain_override wins over auto-inference.
    domain_profile, _domain_meta = resolve_campaign_domain_profile(campaign)
    if _domain_meta.get("should_persist"):
        try:
            campaign_ref.update({"system_domain_profile": domain_profile})
        except Exception as _profile_write_err:
            log.warning(
                "produce_domain_profile_write_failed",
                campaign_id=campaign_id,
                error=str(_profile_write_err),
            )
    campaign["system_domain_profile"] = domain_profile
    if _domain_meta.get("override_active"):
        log.info(
            "produce_domain_override_active",
            campaign_id=campaign_id,
            domain_family=domain_profile.get("domain_family"),
            source=_domain_meta.get("source"),
            strictness_bias=domain_profile.get("strictness_bias"),
            note="Manual domain_override is active; auto-inference skipped.",
        )
    elif _domain_meta.get("error"):
        log.warning(
            "produce_domain_override_invalid",
            campaign_id=campaign_id,
            error=_domain_meta.get("error"),
            note="Invalid domain_override ignored; fell back to auto-inference.",
        )
    if domain_profile.get("thin_campaign") or str(
        domain_profile.get("profile_confidence") or ""
    ).lower() == "low":
        log.info(
            "produce_domain_thin_profile",
            campaign_id=campaign_id,
            domain_family=domain_profile.get("domain_family"),
            confidence=domain_profile.get("confidence"),
            profile_confidence=domain_profile.get("profile_confidence"),
            input_richness=domain_profile.get("input_richness"),
            soft_domain_adjustments=bool(domain_profile.get("soft_domain_adjustments")),
            strictness_bias=domain_profile.get("strictness_bias"),
            note="Thin/low-confidence domain profile — milder domain adjustments applied.",
        )
    log.info(
        "produce_domain_profile_loaded",
        campaign_id=campaign_id,
        domain_family=domain_profile.get("domain_family"),
        confidence=domain_profile.get("confidence"),
        profile_confidence=domain_profile.get("profile_confidence"),
        thin_campaign=bool(domain_profile.get("thin_campaign")),
        input_richness=domain_profile.get("input_richness"),
        liquidity_level=domain_profile.get("liquidity_level"),
        low_liquidity=bool(domain_profile.get("low_liquidity_market")),
        strictness_bias=domain_profile.get("strictness_bias"),
        preferred_sources=domain_profile.get("preferred_sources"),
        override_active=bool(_domain_meta.get("override_active")),
        domain_source=_domain_meta.get("source"),
    )

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

    # V26: Extract intelligence_strategy fields for query_brain
    _intel_strategy = campaign.get("intelligence_strategy") or {}
    _vocab_notes = ""
    if isinstance(_intel_strategy, dict):
        _vocab_notes = (_intel_strategy.get("vocabulary_notes") or "").strip()

    try:
        smart_keywords = generate_smart_query(
            keywords, tenant_id, bio, sourcing_vector,
            persona_category=_persona_cat,
            targeting_signals=_targeting_signals,
            campaign_id=campaign_id,
            force_query_refresh=bool(campaign.get("_force_query_refresh")),
            vocabulary_notes=_vocab_notes,
            intelligence_strategy=_intel_strategy if _intel_strategy else None,
            campaign_name=(campaign.get("name") or ""),
            location=location,
            pain_point=(campaign.get("pain_point") or ""),
            domain_profile=domain_profile if isinstance(domain_profile, dict) else None,
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
    _query_memory_cap = 80
    _prior_query_memory = campaign.get("_query_novelty_memory_signatures") or []
    _prior_query_memory = [str(sig).strip() for sig in _prior_query_memory if str(sig).strip()]
    _executed_query_signatures: list[str] = []

    def _persist_query_memory() -> None:
        if not _executed_query_signatures:
            return
        merged: list[str] = []
        seen: set[str] = set()
        for sig in _executed_query_signatures + _prior_query_memory:
            if not sig or sig in seen:
                continue
            seen.add(sig)
            merged.append(sig)
            if len(merged) >= _query_memory_cap:
                break
        try:
            campaign_ref.update({
                "_query_novelty_memory_signatures": merged,
                "_query_novelty_memory_updated_at": firestore.SERVER_TIMESTAMP,
            })
        except Exception as _memory_exc:
            log.warning(
                "produce_query_memory_update_failed",
                campaign_id=campaign_id,
                error=str(_memory_exc),
            )

    def _run_signal_harvest_pathway(campaign_snapshot: dict) -> dict:
        """Run multi-source signal harvest with a bounded wait for metrics."""
        import os as _os
        import threading as _threading

        harvest_metrics: dict = {}
        _harvest_enabled = _os.environ.get("HARVEST_ENABLED", "true").lower() != "false"
        if not _harvest_enabled:
            return harvest_metrics

        _serper_key_for_harvest = ""
        try:
            from core.clients import get_serper_key  # type: ignore[import]
            _serper_key_for_harvest = get_serper_key() or ""
        except Exception:
            pass  # SerperDiscoverySource will be skipped without a key

        _campaign_with_id = {
            **campaign_snapshot,
            "id": campaign_id,
            "tenant_id": tenant_id,
        }
        harvest_result_holder: list[dict] = []

        def _run_harvest() -> None:
            try:
                from services.signal_harvest import harvest_signals  # type: ignore[import]
                result = harvest_signals(
                    campaign=_campaign_with_id,
                    db=get_db(),
                    serper_api_key=_serper_key_for_harvest,
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
                note="Harvest exceeded 300s wait budget. Continuing without harvest metrics for this response.",
            )

        return harvest_metrics

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
        harvest_metrics = _run_signal_harvest_pathway(campaign)
        log.warning(
            "produce_all_queries_sanitized_empty",
            campaign_id=campaign_id,
            note="All generated queries were system junk. Running signal_harvest fallback.",
        )
        return jsonify({
            "status": "produced",
            "fetched": 0,
            "deduplicated": 0,
            "queued": 0,
            "queue_depth": len(campaign.get("unprocessed_queue", [])),
            "warning": "All queries sanitized as system junk.",
            "harvest": harvest_metrics,
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

    # ------------------------------------------------------------------
    # V26 (Task 2.4): Case-insensitive deduplication of smart_keywords.
    # Colloquial translation and multi-strategy queries can produce near-
    # duplicate queries that waste Serper credits for identical results.
    # ------------------------------------------------------------------
    _seen_queries: set[str] = set()
    _deduped_keywords: list[str] = []
    for _kw in smart_keywords:
        _dedup_key = _kw.strip().lower()
        if _dedup_key not in _seen_queries:
            _seen_queries.add(_dedup_key)
            _deduped_keywords.append(_kw)
    _dedup_dropped = len(smart_keywords) - len(_deduped_keywords)
    if _dedup_dropped > 0:
        log.info(
            "produce_query_dedup",
            campaign_id=campaign_id,
            original_count=len(smart_keywords),
            deduped_count=len(_deduped_keywords),
            dropped=_dedup_dropped,
            note="Case-insensitive dedup removed duplicate queries before Serper loop.",
        )
    smart_keywords = _deduped_keywords
    _governed = govern_query_portfolio(
        smart_keywords,
        campaign=campaign,
        sourcing_vector=sourcing_vector,
        location=location,
    )
    smart_keywords = _governed.get("queries", []) or []
    _govern_stats = _governed.get("stats", {}) or {}
    log.info(
        "produce_query_governance_applied",
        campaign_id=campaign_id,
        **_govern_stats,
    )
    # Domain portfolio shaping runs AFTER governance and BEFORE Serper /
    # exhaustion escalation so preferred platforms and blocked subreddits
    # win over generic query mix without fighting governance caps.
    _kw_for_domain = ""
    if isinstance(keywords, list):
        _kw_for_domain = ", ".join(str(k) for k in keywords if k)
    else:
        _kw_for_domain = str(
            campaign.get("persona_keywords") or campaign.get("keywords") or ""
        )
    _domain_profiled = apply_domain_query_profile(
        smart_keywords,
        domain_profile if isinstance(domain_profile, dict) else None,
        location=location or "",
        keywords=_kw_for_domain,
    )
    smart_keywords = _domain_profiled.get("queries", []) or []
    _dom_dropped = int(_domain_profiled.get("dropped") or 0)
    _dom_injected = int(_domain_profiled.get("injected") or 0)
    _dom_boosted = int(_domain_profiled.get("boosted") or 0)
    _dom_reordered = bool(_domain_profiled.get("reordered"))
    if _dom_dropped or _dom_injected or _dom_boosted or _dom_reordered:
        log.info(
            "produce_domain_query_profile_applied",
            campaign_id=campaign_id,
            domain_family=_domain_profiled.get("domain_family")
            or (domain_profile.get("domain_family") if isinstance(domain_profile, dict) else None),
            dropped=_dom_dropped,
            injected=_dom_injected,
            boosted=_dom_boosted,
            reordered=_dom_reordered,
            preferred_hints=(
                (domain_profile.get("preferred_query_hints") or [])[:5]
                if isinstance(domain_profile, dict)
                else []
            ),
            preferred_sources=(
                (domain_profile.get("preferred_sources") or [])[:5]
                if isinstance(domain_profile, dict)
                else []
            ),
            remaining=len(smart_keywords),
            note="Domain profile shaped governed queries before Serper execution.",
        )
    else:
        log.info(
            "produce_domain_query_profile_noop",
            campaign_id=campaign_id,
            domain_family=(
                domain_profile.get("domain_family")
                if isinstance(domain_profile, dict)
                else None
            ),
            remaining=len(smart_keywords),
            note="No domain query adjustments needed (or no domain profile signals).",
        )
    _escalation_level = int(campaign.get("_query_exhaustion_escalation_level") or 0)
    if _escalation_level > 0:
        _escalation_queries = build_exhaustion_escalation_queries(
            campaign=campaign,
            location=location,
            level=_escalation_level,
        )
        if _escalation_queries:
            smart_keywords = _escalation_queries + smart_keywords
            log.info(
                "produce_query_exhaustion_escalation_applied",
                campaign_id=campaign_id,
                escalation_level=_escalation_level,
                injected=len(_escalation_queries),
            )

    _memory_filtered = filter_queries_against_memory(
        smart_keywords,
        prior_signatures=_prior_query_memory,
        keep_minimum=2,
    )
    smart_keywords = _memory_filtered.get("queries", []) or []
    if int(_memory_filtered.get("dropped") or 0) > 0:
        log.info(
            "produce_query_memory_filter_applied",
            campaign_id=campaign_id,
            dropped=int(_memory_filtered.get("dropped") or 0),
            kept=int(_memory_filtered.get("kept") or 0),
        )
    if not smart_keywords:
        log.warning(
            "produce_query_governance_empty",
            campaign_id=campaign_id,
            note="Governance removed/trimmed all candidate queries. Triggering harvest fallback.",
        )
        harvest_metrics = _run_signal_harvest_pathway(campaign)
        _empty_domain_impact = build_domain_impact_summary(
            domain_profile if isinstance(domain_profile, dict) else None,
            query_stats={
                "dropped": _dom_dropped,
                "injected": _dom_injected,
                "boosted": _dom_boosted,
                "reordered": _dom_reordered,
                "domain_family": (
                    domain_profile.get("domain_family")
                    if isinstance(domain_profile, dict)
                    else None
                ),
            },
            cycle="produce",
            extra={"fetched": 0, "queued": 0, "query_count": 0, "empty_portfolio": True},
        )
        log.info(
            "produce_domain_impact_summary",
            campaign_id=campaign_id,
            domain_family=_empty_domain_impact.get("domain_family"),
            confidence=_empty_domain_impact.get("confidence"),
            strictness_bias=_empty_domain_impact.get("strictness_bias"),
            queries_dropped=_empty_domain_impact.get("queries_dropped"),
            queries_injected=_empty_domain_impact.get("queries_injected"),
            queries_boosted=_empty_domain_impact.get("queries_boosted"),
            queries_reordered=_empty_domain_impact.get("queries_reordered"),
            note="End-of-produce domain impact (empty query portfolio after governance).",
        )
        return jsonify({
            "status": "produced",
            "fetched": 0,
            "deduplicated": 0,
            "queued": 0,
            "queue_depth": len(campaign.get("unprocessed_queue", [])),
            "warning": "No governed queries available.",
            "harvest": harvest_metrics,
            "domain_impact_summary": _empty_domain_impact,
        }), 200

    for kw in smart_keywords:
        clean_location = location if location and location.lower() != "all" else ""
        search_query   = kw

        # F2 (V25.6.1): Query quality gate — reject known garbage patterns
        # before they consume Serper credits. query_brain occasionally generates
        # queries that echo back social URLs, N/A literals, or numbered list
        # fragments from scraped content (e.g. "quora.com 1. Oman Reality user").
        _q_lower = search_query.lower().strip()
        _GARBAGE_PATTERNS = (
            "n/a", "none", "null", "undefined", "unknown",
            "1. ", "2. ", "3. ",  # numbered list fragments from scraped content
        )
        _ECHO_DOMAINS = (
            "quora.com", "reddit.com", "facebook.com", "youtube.com",
            "linkedin.com", "twitter.com", "x.com", "instagram.com",
        )
        _is_garbage = (
            len(_q_lower) < 10
            or _q_lower in _GARBAGE_PATTERNS
            or any(_q_lower.startswith(p) for p in _GARBAGE_PATTERNS)
            # Detect echo queries: "quora.com <scraped title>" or
            # "\"quora.com\" <snippet>" that just re-search the source platform
            or any(
                _q_lower.startswith(f'"{d}"') or _q_lower.startswith(d)
                for d in _ECHO_DOMAINS
            )
        )
        if _is_garbage:
            log.info(
                "produce_query_quality_gate",
                query=search_query[:80],
                campaign_id=campaign_id,
                note="Garbage query blocked before Serper call — saves 1 credit.",
            )
            continue

        _executed_query_signatures.append(query_signature(search_query))

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
            # V26.0.4.4: Only retry globally if the query is a structured dork
            # (contains POSITIVE site: operators, OR booleans, or quoted phrases).
            # Natural-language colloquial queries ("cheap property Muscat")
            # return 0 results on BOTH geo and global indexes — retrying
            # globally just burns a second Serper credit for nothing.
            # V26.0.4.5 FIX: Must extract the QUERY BODY only (before the
            # blacklist). The blacklist contains "-site:", -"login", -"our
            # services" etc. — their quote chars and site: operators were
            # making EVERY query look "structured", defeating the guard.
            import re as _re_produce
            _query_body = _re_produce.split(r'\s+-(?:site:|wiki\b|jobs\b|careers\b|investors\b|directory\b|listicle\b|")', search_query, maxsplit=1)[0].strip()
            _is_platform_query = bool(_re_produce.search(r'(?<!\-)site:', _query_body))
            if not raw_results and gl and _is_platform_query:
                log.info(
                    "produce_geo_fallback",
                    query=search_query[:80],
                    original_gl=gl,
                    sourcing_vector=sourcing_vector,
                    note="Consumer geo-restricted platform query returned 0 results. "
                         "Retrying once on global index.",
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
            elif not raw_results and gl and not _is_platform_query:
                log.info(
                    "produce_geo_fallback_skipped",
                    query=search_query[:80],
                    original_gl=gl,
                    sourcing_vector=sourcing_vector,
                    note="Non-platform query returned 0 on geo — skipping global retry "
                         "to avoid duplicate credit burn.",
                    campaign_id=campaign_id,
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

        _raw_count = len(raw_results) if raw_results else 0
        filtered = filter_serper_noise(raw_results)
        _filtered_count = len(filtered)
        _new_count = 0
        _rejected_stale = 0
        for r in filtered:
            link = r.get("link")
            if not link or link in raw_urls:
                continue
            # V25.5.0: Content age filter — reject stale Reddit/forum posts
            if _is_stale_content(r, _is_consumer_vector):
                _rejected_stale += 1
                continue
            raw_urls.append(link)
            _new_count += 1
            snippet_db[link] = {
                "title":   r.get("title", ""),
                "snippet": r.get("snippet", ""),
                "query":   search_query,
            }
        log.info("produce_serper_query_result",
                 query=search_query[:120],
                 campaign_id=campaign_id,
                 raw=_raw_count,
                 after_noise_filter=_filtered_count,
                 rejected_stale=_rejected_stale,
                 new_urls=_new_count,
                 cumulative=len(raw_urls))

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
                    "cached_at":  firestore.SERVER_TIMESTAMP,
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
            .select(["url", "createdAt", "status"])
            .limit(_DEDUP_SCAN_LIMIT)
            .stream()
        )
        _dedup_recrawl_days = max(1, min(120, int(os.environ.get("DEDUP_RECRAWL_DAYS", "30"))))
        _dedup_cutoff = datetime.now(timezone.utc) - timedelta(days=_dedup_recrawl_days)
        if len(known_docs) == _DEDUP_SCAN_LIMIT:
            log.warning("produce_dedup_scan_cap_hit",
                        tenant_id=tenant_id,
                        limit=_DEDUP_SCAN_LIMIT,
                        note="Dedup scan capped. Tenant may have >500 leads. "
                             "Implement cursor pagination (SF-005) to prevent re-scrape.")
        for doc in known_docs:
            lead_data = doc.to_dict() or {}
            u = lead_data.get("url", "")
            _created_at = lead_data.get("createdAt")
            _status = str(lead_data.get("status") or "").strip().lower()
            if u:
                if not _is_recent_for_dedup(_created_at, _dedup_cutoff):
                    continue
                if _status in {
                    "scored_out",
                    "rlhf_filtered",
                    "failed",
                    "failed_scrape",
                    "failed_eval",
                    "failed_vertex_timeout",
                }:
                    continue
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
    # V25.5.0: Query exhaustion detection
    # If 0 new URLs after dedup for 3+ consecutive cycles, the market is
    # saturated or queries are stale. Log a warning and set a flag for
    # query_brain to generate fresh query angles next cycle.
    # ------------------------------------------------------------------
    _exhaustion_counter_field = "_query_exhaustion_consecutive_zeros"
    _exhaustion_level_field = "_query_exhaustion_escalation_level"
    if queued_count == 0:
        _prev_zeros = campaign.get(_exhaustion_counter_field, 0)
        _new_zeros = _prev_zeros + 1
        _prev_level = int(campaign.get(_exhaustion_level_field) or 0)
        _next_level = _prev_level
        _update_payload: dict[str, object] = {_exhaustion_counter_field: _new_zeros}
        if _new_zeros >= 2:
            _next_level = min(_prev_level + 1, 3)
            _update_payload[_exhaustion_level_field] = _next_level
            _update_payload["_force_query_refresh"] = True
        try:
            campaign_ref.update(_update_payload)
        except Exception:
            pass  # non-fatal metadata
        if _new_zeros >= 3:
            log.warning(
                "produce_query_exhaustion_detected",
                campaign_id=campaign_id,
                consecutive_zero_cycles=_new_zeros,
                note="Market may be saturated or queries are stale. "
                     "query_brain should generate fresh query angles.",
            )
            log.warning(
                "produce_query_exhaustion_escalation",
                campaign_id=campaign_id,
                consecutive_zero_cycles=_new_zeros,
                escalation_level=_next_level,
            )
    else:
        # Reset counter on successful produce
        if campaign.get(_exhaustion_counter_field, 0) > 0 or int(campaign.get(_exhaustion_level_field) or 0) > 0:
            try:
                campaign_ref.update({
                    _exhaustion_counter_field: 0,
                    _exhaustion_level_field: 0,
                    "_force_query_refresh": firestore.DELETE_FIELD,
                })
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Write to unprocessed_queue (atomic ArrayUnion, cap at 200)
    # RACE-01/02 FIX: Use firestore.ArrayUnion for atomic, race-safe
    # append instead of destructive overwrite that loses concurrent writes.
    # ------------------------------------------------------------------
    current_queue = campaign.get("unprocessed_queue", [])

    # V24.4 (L3-4): Queue backpressure — if queue depth > 150 unconsumed URLs,
    # skip producing new URLs. The consumer hasn't caught up yet. Producing more
    # would cause the 200-URL cap to silently discard fresh signals.
    _queue_depth = len(current_queue) if current_queue else 0
    if _queue_depth > 150:
        _persist_query_memory()
        log.info(
            "produce_skipped_queue_full",
            campaign_id=campaign_id,
            queue_depth=_queue_depth,
            threshold=150,
            note="Queue saturated. Skipping produce run — consumer must drain queue first. "
                 "Reduce drip_interval_minutes or increase dispatch frequency.",
        )
        return jsonify({"status": "skipped_queue_full", "queue_depth": _queue_depth}), 200

    # Cap fresh_urls to stay within the 200-URL queue limit.
    # Estimate remaining capacity from the snapshot (best-effort — concurrent
    # producers may have appended since the read, but ArrayUnion is idempotent
    # so duplicates are harmless).
    _remaining_capacity = max(200 - _queue_depth, 0)
    _capped_fresh = fresh_urls[:_remaining_capacity] if fresh_urls else []

    if not _capped_fresh:
        _persist_query_memory()
        log.info(
            "produce_no_fresh_after_cap",
            campaign_id=campaign_id,
            queue_depth=_queue_depth,
            fresh_count=len(fresh_urls),
            note="No fresh URLs fit within 200-URL cap.",
        )
        return jsonify({"status": "skipped_no_fresh", "queue_depth": _queue_depth}), 200
    else:
        queued_count = len(_capped_fresh)  # Update queued_count to reflect actual appended

    try:
        import datetime
        update_data = {
            "unprocessed_queue": firestore.ArrayUnion(_capped_fresh),
            "last_produced_at":  firestore.SERVER_TIMESTAMP,
        }
        # V24.4 (L3-5): Always update next_drip_due when the queue is refreshed,
        # not only on first fill. A stale next_drip_due causes immediate dispatch
        # on every sweep instead of respecting the configured drip cadence.
        if _capped_fresh:
            update_data["next_drip_due"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        campaign_ref.update(update_data)

        # Post-write cap enforcement: re-read queue length and trim if another
        # concurrent producer pushed it past 200 (defense-in-depth).
        try:
            _refreshed = campaign_ref.get().to_dict() or {}
            _post_queue = _refreshed.get("unprocessed_queue", [])
            if len(_post_queue) > 200:
                log.warning(
                    "produce_queue_over_cap_trimming",
                    campaign_id=campaign_id,
                    queue_len=len(_post_queue),
                    note="Concurrent append pushed queue past 200. Trimming to 200.",
                )
                campaign_ref.update({"unprocessed_queue": _post_queue[:200]})
        except Exception as _cap_check_err:
            log.warning(
                "produce_queue_cap_check_failed",
                campaign_id=campaign_id,
                error=str(_cap_check_err),
                note="Non-fatal — queue may temporarily exceed 200 URLs.",
            )
    except Exception as exc:
        log.critical(
            "produce_queue_write_failed",
            campaign_id=campaign_id,
            error=str(exc),
            exc_info=True,
        )
        return jsonify({"error": "Queue write failed", "details": str(exc)}), 500

    _persist_query_memory()
    combined_queue = current_queue + _capped_fresh  # For response metrics only

    # ------------------------------------------------------------------
    # V25.1.0: Signal Harvest — multi-source intent discovery pathway.
    # Runs after Serper queue write so it cannot block query production.
    # ------------------------------------------------------------------
    harvest_metrics = _run_signal_harvest_pathway(campaign)

    # Domain impact summary for this produce cycle (query shaping focus).
    _produce_domain_impact = build_domain_impact_summary(
        domain_profile if isinstance(domain_profile, dict) else None,
        query_stats={
            "dropped": _dom_dropped,
            "injected": _dom_injected,
            "boosted": _dom_boosted,
            "reordered": _dom_reordered,
            "domain_family": (
                domain_profile.get("domain_family")
                if isinstance(domain_profile, dict)
                else None
            ),
        },
        cycle="produce",
        extra={
            "fetched": fetched_count,
            "deduplicated": duped_count,
            "queued": len(_capped_fresh),
            "query_count": len(smart_keywords) if isinstance(smart_keywords, list) else 0,
        },
    )
    log.info(
        "produce_domain_impact_summary",
        campaign_id=campaign_id,
        domain_family=_produce_domain_impact.get("domain_family"),
        confidence=_produce_domain_impact.get("confidence"),
        strictness_bias=_produce_domain_impact.get("strictness_bias"),
        queries_dropped=_produce_domain_impact.get("queries_dropped"),
        queries_injected=_produce_domain_impact.get("queries_injected"),
        queries_boosted=_produce_domain_impact.get("queries_boosted"),
        queries_reordered=_produce_domain_impact.get("queries_reordered"),
        liquidity_level=_produce_domain_impact.get("liquidity_level"),
        fetched=fetched_count,
        queued=len(_capped_fresh),
        note="End-of-produce domain intelligence impact for this campaign run.",
    )

    log.info("TRACE-DONE: produce() complete.",
             campaign_id=campaign_id, queue_depth=len(_capped_fresh))

    return jsonify({
        "status":        "produced",
        "fetched":       fetched_count,
        "deduplicated":  duped_count,
        "queued":        len(_capped_fresh),
        "queue_depth":   len(combined_queue),
        # V25.1.0: Signal harvest pathway metrics
        "harvest": harvest_metrics,
        "domain_impact_summary": _produce_domain_impact,
    }), 200
