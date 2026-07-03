"""
Signal Harvest — V25.2.0
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
from services.gemini_service import inline_score_signal                      # type: ignore[import]

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

    metrics = {"discovered": 0, "scored": 0, "queued": 0, "prism_enriched": 0, "errors": 0}

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
    # Stage 6 — Gemini inline scoring (parallel)                          #
    # ------------------------------------------------------------------ #
    scored_results: list[tuple[SignalItem, dict]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=_SOURCE_CONCURRENCY) as pool:
        future_to_signal = {
            pool.submit(
                inline_score_signal,
                signal.combined_text(max_chars=6000),
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
# Firestore write
# ---------------------------------------------------------------------------

def _write_to_firestore(
    scored_results: list[tuple[SignalItem, dict]],
    campaign: dict,
    icp_context: str,
    db: Any,
) -> int:
    """Write HIGH/MEDIUM signals to scraped_cache and unprocessed_queue.

    Writes in the same format as produce.py so dispatch.py processes them
    without any changes to its logic.

    Returns:
        Count of signals successfully written to unprocessed_queue.
    """
    campaign_id = campaign.get("id", campaign.get("campaign_id", "unknown"))
    tenant_id   = campaign.get("tenant_id", "")
    queued      = 0
    batch       = db.batch()
    batch_count = 0

    for signal, score in scored_results:
        tier = score.get("tier", "LOW")
        if tier not in ("HIGH", "MEDIUM"):
            continue

        now_ts = datetime.datetime.utcnow()

        # ---- scraped_cache document ----
        # Dispatch reads text from scraped_cache.text to run pre_filter_gemini.
        # By writing full signal content here, the pre_filter runs on real content.
        cache_ref = db.collection(_SCRAPED_CACHE_COLL).document()
        cache_doc = {
            "url":          signal.url,
            "text":         signal.combined_text(max_chars=8000),
            "title":        signal.title,
            "author":       signal.author,
            "source_type":  signal.source_type,
            "campaign_id":  campaign_id,
            "tenant_id":    tenant_id,
            "created_at":   now_ts,
            # Inline score metadata — enriches dispatch's final_score_and_dm context
            "harvest_tier":              tier,
            "harvest_pain_summary":      score.get("pain_summary", ""),
            "harvest_contact_point":     score.get("contact_point", ""),
            "harvest_buyer_quote":       score.get("buyer_language_quote", ""),
            "harvest_archetype_match":   score.get("archetype_match", ""),
            "harvest_geo_match":         score.get("geo_match", False),
            "signal_metadata":           signal.metadata,
        }
        batch.set(cache_ref, cache_doc)
        batch_count += 1

        # ---- unprocessed_queue document ----
        queue_ref = db.collection(_UNPROCESSED_QUEUE_COLL).document()
        queue_doc = {
            "url":          signal.url,
            "campaign_id":  campaign_id,
            "tenant_id":    tenant_id,
            "created_at":   now_ts,
            "source":       "signal_harvest",
            "source_type":  signal.source_type,
            "harvest_tier": tier,
        }
        batch.set(queue_ref, queue_doc)
        batch_count += 1
        queued += 1

        # Commit in batches of 400 operations (Firestore limit is 500)
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

    # Final commit
    if batch_count > 0:
        try:
            batch.commit()
        except Exception as exc:
            log.error(
                "signal_harvest_final_commit_failed",
                campaign_id=campaign_id,
                error=str(exc),
            )

    log.info(
        "signal_harvest_firestore_written",
        campaign_id=campaign_id,
        queued=queued,
    )
    return queued
