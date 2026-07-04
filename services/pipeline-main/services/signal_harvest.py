"""
Signal Harvest — V25.3.0
=========================
Enterprise multi-source intent signal collection pipeline.

This module replaces the Serper-snippet-based produce logic with a
PRISM-first, full-content architecture:

CURRENT (broken):
  Serper → snippet (140 chars) → Gemini on snippet → rejects ~100% → zero leads

NEW (this module):
  Source Router (Gemini-driven) → Multi-source discovery (Reddit/HN/RSS/Jobs)
  → PRISM scrape for thin signals → Gemini inline scoring on FULL content
  → Write HIGH/MEDIUM to scraped_cache + unprocessed_queue

DESIGN PRINCIPLES:
  1. No snippet gating. Gemini scores FULL content.
  2. Source selection is 100% dynamic — derived from campaign ICP by Gemini.
  3. No hardcoded subreddits, queries, or feed URLs.
  4. Works for B2B, B2C, D2C, B2B2C — determined by campaign.sourcing_vector.
  5. Backward compatible — writes to the same Firestore collections as produce.py.
  6. PRISM enrichment runs for thin signals (Serper URLs, link posts).

INTEGRATION:
  Called from produce.py as a parallel signal pathway.
  The existing Serper pathway continues to run for backward compatibility.
  Both pathways write to the same scraped_cache / unprocessed_queue collections.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import os
from typing import Optional, Any

import httpx                           # type: ignore[import]
from bs4 import BeautifulSoup          # type: ignore[import]
from google.cloud import firestore     # type: ignore[import]

from core.logging import get_logger                                          # type: ignore[import]
from core.clients import get_db                                              # type: ignore[import]
from services.context_builder import build_enriched_context                 # type: ignore[import]
from services.source_router import SourceRouter, RoutingConfig               # type: ignore[import]
from services.signal_sources.base import SignalItem                          # type: ignore[import]
from services.gemini_service import inline_score_signal, check_topic_coherence  # type: ignore[import]

log = get_logger("pipeline.signal_harvest")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MAX_SIGNALS_PER_RUN     = int(os.environ.get("HARVEST_MAX_SIGNALS",   "50"))
_MAX_PRISM_SCRAPES       = int(os.environ.get("HARVEST_MAX_PRISM",     "20"))
_PRISM_CONCURRENCY       = int(os.environ.get("HARVEST_PRISM_CONCUR",  "8"))
_SOURCE_CONCURRENCY      = int(os.environ.get("HARVEST_SRC_CONCUR",    "6"))
_PRISM_CONNECT_TIMEOUT   = 8    # seconds
_PRISM_READ_TIMEOUT      = 20   # seconds
_SCRAPED_CACHE_COLL      = "scraped_cache"
_UNPROCESSED_QUEUE_COLL  = "unprocessed_queue"

_SOCIAL_SNIPPET_DOMAINS = frozenset({
    "linkedin.com", "x.com", "twitter.com",
    "facebook.com", "instagram.com", "threads.net",
})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def harvest_signals(
    campaign: dict,
    db: Any,
    serper_api_key: str = "",
) -> dict:
    """Run the full signal harvest pipeline for a campaign.

    Discovers intent signals from multiple open-web sources, scores them
    with full content against the campaign ICP, and writes HIGH/MEDIUM
    signals to Firestore for dispatch processing.

    Args:
        campaign:        Full campaign dict from Firestore.
        db:              Firestore client (from core.clients.get_db()).
        serper_api_key:  Serper API key for URL discovery source.
                         If empty, SerperDiscoverySource is skipped.

    Returns:
        Dict with harvest summary metrics:
          discovered: total signals found across all sources
          scored:     signals that went through Gemini inline scoring
          queued:     signals written to unprocessed_queue (HIGH/MEDIUM)
          prism_enriched: thin signals enriched via PRISM scraping
          errors:     count of sources that failed
    """
    campaign_id = campaign.get("id", campaign.get("campaign_id", "unknown"))
    archetype   = (campaign.get("sourcing_vector") or "B2B").upper()
    geo         = campaign.get("location", "")
    tenant_id   = campaign.get("tenant_id", "")

    log.info(
        "signal_harvest_start",
        campaign_id=campaign_id,
        archetype=archetype,
        geo=geo or "global",
    )

    metrics = {"discovered": 0, "scored": 0, "queued": 0, "direct_leads": 0, "prism_enriched": 0, "errors": 0}

    # ------------------------------------------------------------------ #
    # Stage 1 — Build ICP context                                         #
    # ------------------------------------------------------------------ #
    try:
        icp_context = build_enriched_context(campaign)
    except Exception as exc:
        log.error(
            "signal_harvest_icp_failed",
            campaign_id=campaign_id,
            error=str(exc),
        )
        return metrics

    # ------------------------------------------------------------------ #
    # Stage 2 — Gemini-driven source routing                              #
    # ------------------------------------------------------------------ #
    try:
        router = SourceRouter(serper_api_key=serper_api_key)
        routing: RoutingConfig = router.route(
            archetype   = archetype,
            icp_context = icp_context,
            geo         = geo,
            campaign    = campaign,
        )
    except Exception as exc:
        log.error(
            "signal_harvest_routing_failed",
            campaign_id=campaign_id,
            error=str(exc),
        )
        return metrics

    log.info(
        "signal_harvest_routing_complete",
        campaign_id=campaign_id,
        source_count=len(routing.sources),
        source_types=[s.source_type for s in routing.sources],
        routing_method=routing.derived_by,
    )

    # ------------------------------------------------------------------ #
    # Stage 3 — Multi-source discovery (parallel)                         #
    # ------------------------------------------------------------------ #
    all_signals: list[SignalItem] = []
    error_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=_SOURCE_CONCURRENCY) as pool:
        future_to_source = {pool.submit(src.discover): src for src in routing.sources}
        for future in concurrent.futures.as_completed(future_to_source, timeout=90):
            src = future_to_source[future]
            try:
                batch = future.result()
                all_signals.extend(batch)
                log.info(
                    "signal_harvest_source_done",
                    source_type=src.source_type,
                    signals=len(batch),
                )
            except concurrent.futures.TimeoutError:
                error_count += 1
                log.warning(
                    "signal_harvest_source_timeout",
                    source_type=src.source_type,
                )
            except Exception as exc:
                error_count += 1
                log.warning(
                    "signal_harvest_source_error",
                    source_type=src.source_type,
                    error=str(exc),
                )

    metrics["discovered"] = len(all_signals)
    metrics["errors"]     = error_count

    # ------------------------------------------------------------------ #
    # V25.2.1 — Stamp last_google_reviews_at if reviews ran this harvest  #
    # Enables the once-daily cooldown gate in source_router._instantiate_  #
    # sources(). Only written when reviews actually ran (signals > 0 OR   #
    # source was active, i.e. GoogleReviewSource was in routing.sources). #
    # ------------------------------------------------------------------ #
    _reviews_ran = any(
        s.source_type == "google_review" for s in routing.sources
    )
    if _reviews_ran:
        try:
            camp_ref = db.collection("campaigns").document(campaign_id)
            camp_ref.update({
                "last_google_reviews_at": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
            })
            log.info(
                "signal_harvest_google_reviews_timestamp_stamped",
                campaign_id=campaign_id,
                note="Cooldown clock reset. Google Reviews will skip next 23h of harvests.",
            )
        except Exception as _stamp_err:
            log.warning(
                "signal_harvest_google_reviews_timestamp_failed",
                campaign_id=campaign_id,
                error=str(_stamp_err),
                note="Non-critical — cooldown will not apply next harvest.",
            )

    if not all_signals:
        log.warning(
            "signal_harvest_no_signals",
            campaign_id=campaign_id,
            archetype=archetype,
            sources=len(routing.sources),
        )
        return metrics


    # ------------------------------------------------------------------ #
    # Stage 4 — Deduplicate against existing leads/cache                  #
    # ------------------------------------------------------------------ #
    fresh_signals = _dedup_against_cache(all_signals, campaign_id, tenant_id, db)

    log.info(
        "signal_harvest_dedup_complete",
        campaign_id=campaign_id,
        total=len(all_signals),
        fresh=len(fresh_signals),
        deduplicated=len(all_signals) - len(fresh_signals),
    )

    # Cap signals to prevent runaway cost
    fresh_signals = fresh_signals[:_MAX_SIGNALS_PER_RUN]

    # ------------------------------------------------------------------ #
    # Stage 4.5 — Social snippet injection (V25.2.0)                      #
    # For social-domain URLs found via Serper, use the cached Google       #
    # snippet as signal text. PRISM cannot access these without auth.      #
    # ------------------------------------------------------------------ #
    fresh_signals = _inject_social_snippets(fresh_signals)

    # ------------------------------------------------------------------ #
    # Stage 5 — PRISM enrichment for thin signals                         #
    # ------------------------------------------------------------------ #
    thin_signals  = [s for s in fresh_signals if s.is_thin][:_MAX_PRISM_SCRAPES]
    rich_signals  = [s for s in fresh_signals if not s.is_thin]

    if thin_signals:
        enriched = _prism_enrich_batch(thin_signals)
        metrics["prism_enriched"] = len(enriched)
        # Merge: replace thin signals with enriched versions
        enriched_urls = {e.url: e for e in enriched}
        rich_signals.extend(
            enriched_urls.get(s.url, s)   # use enriched if available, else original
            for s in thin_signals
        )
    else:
        rich_signals = fresh_signals

    # ------------------------------------------------------------------ #
    # Stage 5.5 — Topic coherence gate (V25.5.0)                           #
    # Cheap Gemini Flash YES/NO check: "Is this content PRIMARILY about    #
    # the campaign topic?" Rejects signals that only mention the topic     #
    # incidentally (e.g., geopolitics thread mentioning "Oman" in passing).#
    # Runs BEFORE the expensive full scoring to save Gemini credits.       #
    # ------------------------------------------------------------------ #
    _campaign_topic = (
        campaign.get("bio", "")[:200]
        or " ".join(campaign.get("keywords", [])[:5])
    )
    if _campaign_topic:
        coherent_signals = []
        _coherence_rejected = 0
        for sig in rich_signals:
            _sig_title = sig.title or ""
            _sig_snippet = (sig.combined_text(max_chars=300) or "")[:300]
            if check_topic_coherence(
                title=_sig_title,
                snippet=_sig_snippet,
                campaign_topic=_campaign_topic,
            ):
                coherent_signals.append(sig)
            else:
                _coherence_rejected += 1
                log.info(
                    "signal_harvest_coherence_rejected",
                    url=sig.url[:80],
                    title=_sig_title[:80],
                    campaign_id=campaign_id,
                )
        if _coherence_rejected > 0:
            log.info(
                "signal_harvest_coherence_gate_summary",
                campaign_id=campaign_id,
                input_count=len(rich_signals),
                passed=len(coherent_signals),
                rejected=_coherence_rejected,
            )
        metrics["coherence_rejected"] = _coherence_rejected
        rich_signals = coherent_signals

    # ------------------------------------------------------------------ #
    # Stage 6 — Gemini inline scoring (parallel)                          #
    # ------------------------------------------------------------------ #
    scored_results: list[tuple[SignalItem, dict]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=_SOURCE_CONCURRENCY) as pool:
        future_to_signal = {
            pool.submit(
                inline_score_signal,
                # 3C: Reddit content cap — use first 1500 chars for reddit.com
                # URLs to focus Gemini scoring on the OP primary post instead
                # of deep comment chains (comment #347 is noise, not intent).
                signal.combined_text(
                    max_chars=1500 if "reddit.com" in signal.url else 6000
                ),
                icp_context,
                signal.url,
                signal.source_type,
                geo,
                archetype,
                routing.buyer_language_context,
            ): signal
            for signal in rich_signals
        }
        for future in concurrent.futures.as_completed(future_to_signal, timeout=120):
            signal = future_to_signal[future]
            try:
                score = future.result()
                metrics["scored"] += 1
                scored_results.append((signal, score))
            except Exception as exc:
                log.warning(
                    "signal_harvest_score_error",
                    url=signal.url[:80],
                    error=str(exc),
                )

    # ------------------------------------------------------------------ #
    # Stage 7 — Write HIGH/MEDIUM to Firestore                            #
    # ------------------------------------------------------------------ #
    queued = _write_to_firestore(
        scored_results = scored_results,
        campaign       = campaign,
        icp_context    = icp_context,
        db             = db,
        metrics        = metrics,
    )
    metrics["queued"] = queued

    # ------------------------------------------------------------------ #
    # Stage 8 — Write ALL scored signals to BQ raw_signals (V25.2.0)     #
    # Non-blocking daemon thread — zero latency impact.                   #
    # BQ accumulates full signal history for cluster analyst.             #
    # ------------------------------------------------------------------ #
    try:
        from services.signal_bq_writer import write_signals_to_bq  # type: ignore[import]
        write_signals_to_bq(scored_results=scored_results, campaign=campaign)
    except Exception as _bq_err:
        log.warning(
            "signal_harvest_bq_write_skipped",
            error=str(_bq_err),
            note="Non-critical — harvest results still written to Firestore.",
        )

    log.info(
        "signal_harvest_complete",
        campaign_id=campaign_id,
        archetype=archetype,
        **metrics,
    )
    return metrics


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedup_against_cache(
    signals: list[SignalItem],
    campaign_id: str,
    tenant_id: str,
    db: Any,
) -> list[SignalItem]:
    """Remove signals already present in scraped_cache or unprocessed_queue."""
    if not signals:
        return []

    # Collect candidate URLs
    urls = list({s.url for s in signals if s.url})

    try:
        # Check scraped_cache (keyed by URL hash in the url field)
        existing_cache: set[str] = set()

        # Firestore 'in' queries are capped at 30 items — batch if needed
        batch_size = 30
        for i in range(0, len(urls), batch_size):
            batch_urls = urls[i: i + batch_size]
            docs = (
                db.collection(_SCRAPED_CACHE_COLL)
                .where(filter=firestore.FieldFilter("url", "in", batch_urls))
                .stream()
            )
            for doc in docs:
                existing_cache.add(doc.to_dict().get("url", ""))

        fresh = [s for s in signals if s.url not in existing_cache]
        return fresh

    except Exception as exc:
        log.warning(
            "signal_harvest_dedup_failed",
            campaign_id=campaign_id,
            error=str(exc),
            note="Returning all signals unfiltered.",
        )
        return signals


# ---------------------------------------------------------------------------
# Social snippet injection (Stage 4.5)
# ---------------------------------------------------------------------------

def _inject_social_snippets(signals: list[SignalItem]) -> list[SignalItem]:
    """Promote Serper snippets to full signal text for social-domain URLs.

    PRISM cannot scrape LinkedIn, X, Facebook, Instagram, or Threads without
    authentication. For these URLs discovered via Serper, the raw Google snippet
    stored in ``metadata["serper_snippet"]`` is the buyer's own words as indexed
    by Google. When the snippet is substantive (≥30 chars) it is promoted to the
    signal's text field so Gemini inline scoring can operate on real content.

    Args:
        signals: List of SignalItems from all discovery sources.

    Returns:
        New list with social-domain thin signals replaced by snippet-enriched
        versions where possible. All other signals are returned unchanged.
    """
    from urllib.parse import urlparse

    result: list[SignalItem] = []
    injected = 0

    for signal in signals:
        if not signal.is_thin:
            result.append(signal)
            continue

        host = urlparse(signal.url).netloc.lower().replace("www.", "")
        # Check if any known social domain is a suffix of the host
        is_social = any(host == d or host.endswith("." + d) for d in _SOCIAL_SNIPPET_DOMAINS)

        if not is_social:
            result.append(signal)
            continue

        snippet = signal.metadata.get("serper_snippet", "") or ""
        if len(snippet) < 30:
            result.append(signal)
            continue

        platform = _detect_social_platform(host)
        enriched = SignalItem(
            url         = signal.url,
            text        = snippet,
            title       = signal.title,
            author      = signal.author,
            source_type = signal.source_type,
            fetched_at  = signal.fetched_at,
            metadata    = {
                **signal.metadata,
                "is_thin_content": False,
                "content_source":  "serper_snippet",
                "social_platform": platform,
            },
        )
        log.debug(
            "social_snippet_injected",
            url=signal.url[:80],
            platform=platform,
            snippet_len=len(snippet),
        )
        result.append(enriched)
        injected += 1

    log.info(
        "social_snippet_injection_complete",
        total=len(signals),
        injected=injected,
    )
    return result


def _detect_social_platform(host: str) -> str:
    """Map a social-domain hostname to a canonical platform identifier.

    Args:
        host: Hostname with ``www.`` already stripped (e.g. ``linkedin.com``).

    Returns:
        Platform string: ``"linkedin"``, ``"x"``, ``"facebook"``,
        ``"instagram"``, ``"threads"``, or ``"social"`` as fallback.
    """
    if "linkedin.com" in host:
        return "linkedin"
    if "x.com" in host or "twitter.com" in host:
        return "x"
    if "facebook.com" in host:
        return "facebook"
    if "instagram.com" in host:
        return "instagram"
    if "threads.net" in host:
        return "threads"
    return "social"


# ---------------------------------------------------------------------------
# PRISM-lite scraping for thin signals
# ---------------------------------------------------------------------------

def _prism_enrich_batch(thin_signals: list[SignalItem]) -> list[SignalItem]:
    """Scrape thin signals (URL-only) to retrieve full page content.

    Uses httpx with a lightweight BeautifulSoup extractor. This is NOT
    the full PrismPipeline (which requires campaign context and DB).
    This is a lightweight text extractor for open-web pages.

    Signals from walled gardens (Reddit, Facebook, etc.) that can't be
    scraped are returned as-is with their thin content.
    """
    _WALLED_GARDENS = frozenset({
        "reddit.com", "facebook.com", "instagram.com",
        "x.com", "twitter.com", "linkedin.com",
    })

    enriched: list[SignalItem] = []

    def _scrape_url(sig: SignalItem) -> SignalItem:
        from urllib.parse import urlparse
        domain = urlparse(sig.url).netloc.lower().replace("www.", "")
        if any(wg in domain for wg in _WALLED_GARDENS):
            # Walled garden — return as-is (can't scrape without auth)
            return sig

        try:
            with httpx.Client(
                timeout=httpx.Timeout(
                    connect=_PRISM_CONNECT_TIMEOUT,
                    read=_PRISM_READ_TIMEOUT,
                    write=10,
                    pool=5,
                ),
                follow_redirects=True,
                headers={"User-Agent": "LeadGenie/25.1.0 content-fetch"},
            ) as client:
                resp = client.get(sig.url)
                if resp.status_code != 200:
                    return sig

                # Extract visible text using BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                # Remove script/style noise
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()

                text = soup.get_text(separator=" ", strip=True)
                # Limit to first 6000 chars
                text = text[:6000]

                if len(text) > 100:
                    from dataclasses import replace
                    return SignalItem(
                        url         = sig.url,
                        text        = text,
                        title       = sig.title,
                        author      = sig.author,
                        source_type = sig.source_type,
                        fetched_at  = sig.fetched_at,
                        metadata    = {
                            **sig.metadata,
                            "is_thin_content": False,
                            "prism_enriched":   True,
                        },
                    )
        except Exception as exc:
            log.warning(
                "prism_enrich_failed",
                url=sig.url[:80],
                error=str(exc),
            )
        return sig

    with concurrent.futures.ThreadPoolExecutor(max_workers=_PRISM_CONCURRENCY) as pool:
        futures = {pool.submit(_scrape_url, sig): sig for sig in thin_signals}
        for future in concurrent.futures.as_completed(futures, timeout=60):
            try:
                enriched.append(future.result())
            except Exception as exc:
                original = futures[future]
                log.warning(
                    "prism_enrich_thread_error",
                    url=original.url[:80],
                    error=str(exc),
                )
                enriched.append(original)

    return enriched


# ---------------------------------------------------------------------------
# LQS — Lead Quality Score (multi-dimensional composite)  [Phase 3A]
# ---------------------------------------------------------------------------

# Reachability weights by source_type.  Social platforms score lower because
# DMing a random Reddit commenter has lower success than contacting a Google
# reviewer who left their name on a public review.
_REACHABILITY_MAP: dict[str, float] = {
    "google_review": 0.8,
    "classified":    0.7,
    "serper":        0.6,
    "rss":           0.5,
    "reddit":        0.4,
}


def _compute_lqs(signal: SignalItem, score: dict) -> dict:
    """Compute Lead Quality Score — multi-dimensional composite.

    Dimensions:
    - topic_coherence (25%): From Gemini's topic_coherence field (0.0-1.0)
    - intent_strength (25%): Derived from raw_score (normalize 1-10 → 0.0-1.0)
    - freshness (20%): Based on signal age (newer = higher)
    - reachability (15%): Based on source_type (social = lower, direct = higher)
    - dm_confidence (15%): Based on whether a real person name was extracted

    Returns:
        Dict with ``lqs_score`` (float 0.0-1.0) and individual dimension
        values keyed as ``lqs_<dimension>``.
    """
    # --- topic_coherence ---
    coherence = float(score.get("topic_coherence", 0.5))
    coherence = max(0.0, min(1.0, coherence))

    # --- intent_strength ---
    raw = score.get("raw_score", 5)
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        raw = 5.0
    intent = min(raw, 10.0) / 10.0

    # --- freshness ---
    freshness = 0.7  # default when no date available
    _sig_date = signal.metadata.get("date") or signal.metadata.get("published_at")
    if _sig_date:
        try:
            if isinstance(_sig_date, str):
                # ISO-8601 parse (handles most formats)
                _parsed_dt = datetime.datetime.fromisoformat(
                    _sig_date.replace("Z", "+00:00")
                )
            elif isinstance(_sig_date, datetime.datetime):
                _parsed_dt = _sig_date
            else:
                _parsed_dt = None

            if _parsed_dt is not None:
                _now = datetime.datetime.now(datetime.timezone.utc)
                if _parsed_dt.tzinfo is None:
                    _parsed_dt = _parsed_dt.replace(tzinfo=datetime.timezone.utc)
                _age_days = (_now - _parsed_dt).total_seconds() / 86400.0
                # Decay: 1.0 at 0 days, 0.0 at 90+ days, linear
                freshness = max(0.0, min(1.0, 1.0 - (_age_days / 90.0)))
        except (ValueError, TypeError, OverflowError):
            pass  # keep default 0.7

    # --- reachability ---
    reachability = _REACHABILITY_MAP.get(signal.source_type, 0.5)

    # --- dm_confidence ---
    _dm_field = (
        score.get("dm", "") or score.get("decision_maker", "") or ""
    )
    if isinstance(_dm_field, dict):
        _dm_field = _dm_field.get("name", "")
    _dm_field = str(_dm_field).strip()
    dm_conf = 0.8 if (_dm_field and _dm_field.lower() != "unknown") else 0.3

    # --- composite ---
    final = (
        coherence    * 0.25
        + intent     * 0.25
        + freshness  * 0.20
        + reachability * 0.15
        + dm_conf    * 0.15
    )

    return {
        "lqs_score":            round(final, 4),
        "lqs_topic_coherence":  round(coherence, 4),
        "lqs_intent":           round(intent, 4),
        "lqs_freshness":        round(freshness, 4),
        "lqs_reachability":     round(reachability, 4),
        "lqs_dm_confidence":    round(dm_conf, 4),
    }


# ---------------------------------------------------------------------------
# Firestore write
# ---------------------------------------------------------------------------

def _write_to_firestore(
    scored_results: list[tuple[SignalItem, dict]],
    campaign: dict,
    icp_context: str,
    db: Any,
    metrics: dict | None = None,
) -> int:
    """Write scored signals to Firestore.

    HIGH-tier google_review signals are promoted to direct leads, bypassing
    dispatch entirely. These signals already contain full buyer language,
    reviewer identity, pain summary, and geo-confirmed competitor context
    — dispatch’s 19 drop points would add latency without new enrichment.

    All other HIGH/MEDIUM signals are written to scraped_cache and appended
    to the campaign document’s ``unprocessed_queue`` array via Firestore
    ``arrayUnion``, matching produce.py’s write path exactly.

    Returns:
        Count of signals written to unprocessed_queue (excludes direct leads).
    """
    campaign_id = campaign.get("id", campaign.get("campaign_id", "unknown"))
    tenant_id   = campaign.get("tenant_id", "")
    queued      = 0
    direct_leads_count = 0
    batch       = db.batch()
    batch_count = 0
    harvest_urls: list[str] = []

    from urllib.parse import urlparse as _urlparse
    from google.api_core.exceptions import AlreadyExists  # type: ignore[import]

    for signal, score in scored_results:
        tier = score.get("tier", "LOW")
        if tier not in ("HIGH", "MEDIUM"):
            continue

        now_ts = datetime.datetime.utcnow()

        # 3A: Compute LQS composite score before writing
        lqs = _compute_lqs(signal, score)

        # ---- scraped_cache document ----
        # Dispatch reads text from scraped_cache.text to run pre_filter_gemini.
        # By writing full signal content here, the pre_filter runs on real content
        # instead of 140-char Serper snippets.
        _parsed = _urlparse(signal.url)
        # Include fragment (#review-0, #review-1) in dedup key — urlparse
        # strips fragments from .path, so reviews sharing a base URL would
        # collide without this.
        _frag_suffix = f"#{_parsed.fragment}" if _parsed.fragment else ""
        _dedup_key = f"{_parsed.netloc}{_parsed.path}{_frag_suffix}".lower().replace("www.", "")
        _cache_key = hashlib.sha256(f"{tenant_id}_{_dedup_key}".encode()).hexdigest()

        cache_ref = db.collection(_SCRAPED_CACHE_COLL).document(_cache_key)
        cache_doc = {
            "url":          signal.url,
            "text":         signal.combined_text(max_chars=8000),
            "title":        signal.title,
            "author":       signal.author,
            "source_type":  signal.source_type,
            "campaign_id":  campaign_id,
            "tenant_id":    tenant_id,
            "created_at":   now_ts,
            # Inline score metadata — enriches dispatch’s final_score_and_dm context
            "harvest_tier":              tier,
            "harvest_pain_summary":      score.get("pain_summary", ""),
            "harvest_contact_point":     score.get("contact_point", ""),
            "harvest_buyer_quote":       score.get("buyer_language_quote", ""),
            "harvest_archetype_match":   score.get("archetype_match", ""),
            "harvest_geo_match":         score.get("geo_match", False),
            "signal_metadata":           signal.metadata,
            # 3A: LQS composite dimensions
            **lqs,
        }
        batch.set(cache_ref, cache_doc, merge=True)
        batch_count += 1

        # ------------------------------------------------------------------ #
        # V25.3.0 — Direct lead creation for HIGH-tier google_review signals  #
        #                                                                      #
        # These signals contain full buyer language (verbatim review text),     #
        # reviewer name, pain summary from inline scoring, geo confirmation,   #
        # and competitor context. Dispatch’s 19 drop points add latency with   #
        # no incremental enrichment for this signal class.                     #
        # ------------------------------------------------------------------ #
        if tier == "HIGH" and signal.source_type == "google_review":
            lead_id = hashlib.sha256(
                f"{tenant_id}_{_dedup_key}".encode()
            ).hexdigest()

            # INT-04 FIX: Set dm from reviewer author (signal.author) and
            # company_name from the reviewer's business context, not the
            # competitor being reviewed.
            _reviewer_name = (signal.author or "").strip()
            _reviewer_snippet = (score.get("pain_summary", "") or "")[:200]
            _dm_value = _reviewer_name if _reviewer_name else "Google Maps Reviewer"
            # company_name should reflect the reviewer's company/context, not
            # the competitor place. Use reviewer metadata if available, else
            # derive from the review pain context.
            _company_from_reviewer = (
                signal.metadata.get("reviewer_company", "")
                or signal.metadata.get("reviewer_context", "")
                or ""
            ).strip()
            _company_name_value = _company_from_reviewer if _company_from_reviewer else f"Reviewer of {signal.metadata.get('place_name', 'Unknown')}"

            # FIN-01 FIX: Check/reserve credits before creating direct lead.
            # Mirror dispatch.py's credit settlement pattern.
            _credits_available = True
            try:
                _user_snap = db.collection("users").document(tenant_id).get()
                _user_data = _user_snap.to_dict() or {} if _user_snap.exists else {}
                _credit_limit = _user_data.get("credit_limit", 0)
                _credits_used = _user_data.get("credits_used", 0)
                if _credit_limit > 0 and _credits_used >= _credit_limit:
                    _credits_available = False
                    log.info(
                        "signal_harvest_direct_lead_insufficient_credits",
                        campaign_id=campaign_id,
                        tenant_id=tenant_id,
                        credits_used=_credits_used,
                        credit_limit=_credit_limit,
                        note="Insufficient credits for direct lead. Queuing URL normally.",
                    )
            except Exception as _cred_check_err:
                log.warning(
                    "signal_harvest_credit_check_failed",
                    campaign_id=campaign_id,
                    error=str(_cred_check_err),
                    note="Credit check failed. Proceeding with lead creation (fail-open).",
                )

            if not _credits_available:
                # Insufficient credits — queue the URL through normal dispatch instead
                harvest_urls.append(signal.url)
                queued += 1
                continue

            _pain_summary = str(score.get("pain_summary", "") or "")
            _buyer_quote = str(score.get("buyer_language_quote", "") or "")
            _contact_point = str(score.get("contact_point", "") or "")

            lead_doc = {
                "url":              signal.url,
                "status":           "new",
                "score":            8,
                "normalized_score": 80,
                "pain_point":       _pain_summary,
                "intent_signal":    _buyer_quote,
                "decision_maker":   {"name": _reviewer_name},
                "company_name":     _company_name_value,
                "source":           "signal_harvest_review",
                "is_cluster_lead":  False,
                "tenant_id":        tenant_id,
                "campaign_id":      campaign_id,
                "matched_campaigns": [{"campaign_id": campaign_id, "raw_score": 8}],
                "contact_endpoints": [{"platform": "google_maps", "uri": signal.url}],
                "dm":               _dm_value,
                "icebreaker_angle": _pain_summary,
                "score_reasoning":  (
                    f"HIGH-tier Google Review on competitor "
                    f"'{signal.metadata.get('place_name', '')}'. "
                    f"Reviewer is a proven buyer in this category."
                ),
                "evidence_chain":   [{
                    "signal_type": "REVIEW_SIGNAL",
                    "evidence":    _buyer_quote,
                    "confidence":  0.85,
                }],
                "confidence_level": "HIGH",
                # 3A: LQS composite dimensions on direct leads
                **lqs,
                "createdAt":        firestore.SERVER_TIMESTAMP,
                "updatedAt":        firestore.SERVER_TIMESTAMP,
            }

            try:
                lead_ref = db.collection("leads").document(lead_id)
                lead_ref.create(lead_doc)
                direct_leads_count += 1

                # FIN-01 FIX: Settle credit after successful direct lead creation
                try:
                    import random as _rnd
                    _shard = _rnd.randint(0, 9)
                    db.collection("users").document(tenant_id) \
                        .collection("wallet_shards").document(str(_shard)) \
                        .set({"consumed_credits": firestore.Increment(1)}, merge=True)
                except Exception as _settle_err:
                    log.warning(
                        "signal_harvest_credit_settle_failed",
                        campaign_id=campaign_id,
                        lead_id=lead_id,
                        error=str(_settle_err),
                        note="Credit settlement failed after direct lead creation.",
                    )

                log.info(
                    "signal_harvest_direct_lead_created",
                    campaign_id=campaign_id,
                    lead_id=lead_id,
                    place_name=str(signal.metadata.get("place_name", ""))[:80],
                    reviewer=str(signal.author or "")[:60],
                )
            except AlreadyExists:
                # RACE-06 FIX: Release lock on AlreadyExists path.
                # The dedup key was already claimed — release any lock we may
                # hold to prevent lock leak.
                try:
                    _lock_key = hashlib.sha256(_dedup_key.encode()).hexdigest()
                    db.collection("global_lead_locks").document(_lock_key).delete()
                except Exception as _lock_release_err:
                    log.warning(
                        "signal_harvest_lock_release_failed_on_dup",
                        lead_id=lead_id,
                        error=str(_lock_release_err),
                    )
                log.info(
                    "signal_harvest_direct_lead_exists",
                    campaign_id=campaign_id,
                    lead_id=lead_id,
                    note="Dedup: lead already exists, skipping.",
                )
            except Exception as exc:
                log.error(
                    "signal_harvest_direct_lead_failed",
                    campaign_id=campaign_id,
                    lead_id=lead_id,
                    error=str(exc),
                )
                # FALLBACK: Queue URL through normal dispatch instead of losing it
                harvest_urls.append(signal.url)
                queued += 1
            # HIGH-tier reviews bypass the queue — do NOT append to harvest_urls
            continue

        # MEDIUM-tier (and non-review HIGH) signals go through dispatch queue
        harvest_urls.append(signal.url)
        queued += 1

        # Commit scraped_cache in batches of 400 operations (Firestore limit is 500)
        if batch_count >= 400:
            try:
                batch.commit()
            except Exception as exc:
                log.error(
                    "signal_harvest_batch_commit_failed",
                    campaign_id=campaign_id,
                    error=str(exc),
                )
            batch       = db.batch()
            batch_count = 0

    # Track direct lead count in caller’s metrics dict
    if metrics is not None:
        metrics["direct_leads"] = direct_leads_count

    # Final scraped_cache commit
    if batch_count > 0:
        try:
            batch.commit()
        except Exception as exc:
            log.error(
                "signal_harvest_final_commit_failed",
                campaign_id=campaign_id,
                error=str(exc),
            )

    # ---- Append URLs to campaign document’s unprocessed_queue array ----
    # V25.2.4: This is the critical fix. Dispatch reads from the campaign
    # document’s unprocessed_queue array field, NOT from a top-level collection.
    # Use arrayUnion for atomic, dedup-safe append.
    if harvest_urls:
        try:
            campaign_ref = db.collection("campaigns").document(campaign_id)
            # Cap at 200 to match produce.py’s queue limit
            campaign_ref.update({
                "unprocessed_queue": firestore.ArrayUnion(harvest_urls[:200]),
            })
            log.info(
                "signal_harvest_queue_appended",
                campaign_id=campaign_id,
                urls_appended=len(harvest_urls),
            )
        except Exception as exc:
            log.error(
                "signal_harvest_queue_append_failed",
                campaign_id=campaign_id,
                error=str(exc),
                urls_lost=len(harvest_urls),
            )
            queued = 0  # None were actually queued

    log.info(
        "signal_harvest_firestore_written",
        campaign_id=campaign_id,
        queued=queued,
        direct_leads=direct_leads_count,
    )
    return queued

