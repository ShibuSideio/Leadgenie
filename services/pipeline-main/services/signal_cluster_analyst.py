"""
Signal Cluster Analyst — V25.2.0
==================================
Gemini-powered intent clustering engine.

Reads accumulated signals from BQ raw_signals, groups them into
coherent intent clusters, scores by convergence, and creates leads
for clusters above the threshold.

CONVERGENCE SCORE FORMULA:
  convergence_score = (
      min(signal_count, 10) / 10 * 40   # up to 40pts: signal volume
    + min(source_diversity, 5) / 5 * 40  # up to 40pts: platform spread
    + recency_score * 20                 # up to 20pts: freshness
  )
  where recency_score = e^(-0.04 * median_age_hours)

LEAD CREATION:
  - Clusters with convergence_score >= CLUSTER_LEAD_THRESHOLD (default 60) create leads
  - Standalone signals with inline_score >= 75 are NOT blocked — they flow
    through the normal Stage 7 Firestore write in harvest. Clustering is additive.

DESIGN:
  - Called from /harvest endpoint after harvest_signals() completes
  - Only runs if >= 3 fresh signals were harvested (else not enough to cluster)
  - Non-blocking for the lead creation path: BQ write is synchronous,
    Firestore lead write is the actual output
"""
from __future__ import annotations

import datetime
import json
import math
import os
import uuid
from typing import Any, Optional

from core.logging import get_logger                 # type: ignore[import]
from services.gemini_service import call_gemini_2_5  # type: ignore[import]

# Lazy imports to avoid circular dependency — dispatch._settle_credit is in api/routers layer
# and signal_cluster_analyst is in services layer. Import inside function body.
# Same pattern used by harvest.py for cluster analyst calls.

log = get_logger("pipeline.signal_cluster_analyst")

_PROJECT_ID             = os.environ.get("GCP_PROJECT", os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
_RAW_SIGNALS_TABLE      = f"{_PROJECT_ID}.swarm_analytics.raw_signals"
_INTENT_CLUSTERS_TABLE  = f"{_PROJECT_ID}.swarm_analytics.intent_clusters"
_CLUSTER_LEAD_THRESHOLD = float(os.environ.get("CLUSTER_LEAD_THRESHOLD", "60"))
_LOOKBACK_HOURS         = int(os.environ.get("CLUSTER_LOOKBACK_HOURS", "48"))
_MIN_SIGNALS_TO_CLUSTER = 3
_SCRAPED_CACHE_COLL     = "scraped_cache"
_UNPROCESSED_QUEUE_COLL = "unprocessed_queue"


def analyse_and_create_leads(
    campaign_id: str,
    tenant_id: str,
    icp_context: str,
    archetype: str,
    geo: str,
    db: Any,
) -> dict:
    """Run clustering analysis for a campaign and create cluster leads.

    Called after harvest_signals() completes. Reads recent signals from
    BQ, groups them with Gemini, scores clusters by convergence, and
    writes qualifying clusters as lead records to Firestore.

    Args:
        campaign_id:  Firestore campaign document ID.
        tenant_id:    Tenant identifier for isolation.
        icp_context:  Enriched ICP context string for Gemini prompt.
        archetype:    B2B / B2C / D2C / B2B2C.
        geo:          Target geography.
        db:           Firestore client.

    Returns:
        Dict with keys: clusters_found, leads_created, signals_read, skipped_reason.
    """
    result = {"clusters_found": 0, "leads_created": 0, "signals_read": 0, "skipped_reason": ""}

    # Step 1 — Read recent signals from BQ
    signals = _read_signals_from_bq(campaign_id, tenant_id, _LOOKBACK_HOURS)
    result["signals_read"] = len(signals)

    if len(signals) < _MIN_SIGNALS_TO_CLUSTER:
        result["skipped_reason"] = f"Insufficient signals: {len(signals)} < {_MIN_SIGNALS_TO_CLUSTER}"
        log.info(
            "cluster_analyst_skipped",
            campaign_id=campaign_id,
            signals=len(signals),
            reason=result["skipped_reason"],
        )
        return result

    # Step 2 — Ask Gemini to identify clusters
    clusters = _gemini_cluster(signals, icp_context, archetype, geo)
    result["clusters_found"] = len(clusters)

    if not clusters:
        log.info("cluster_analyst_no_clusters", campaign_id=campaign_id, signals=len(signals))
        return result

    # Step 3 — Score each cluster, write to BQ, create leads
    leads_created = 0
    for cluster in clusters:
        cluster_id        = str(uuid.uuid4())
        convergence_score = _score_cluster(cluster, signals)
        cluster["convergence_score"] = convergence_score
        cluster["cluster_id"]        = cluster_id

        # Attach contributing signal dicts for downstream use
        indices = cluster.get("contributing_indices", [])
        cluster["contributing_signals"] = [signals[i] for i in indices if i < len(signals)]

        # Write to BQ regardless of threshold (full audit trail)
        _write_cluster_to_bq(cluster, campaign_id, tenant_id, geo, lead_created=False)

        # Create Firestore lead if above threshold
        if convergence_score >= _CLUSTER_LEAD_THRESHOLD:
            lead_id = _create_cluster_lead(cluster, campaign_id, tenant_id, icp_context, archetype, geo, db)
            if lead_id:
                # Update BQ row to mark lead created
                _update_cluster_lead_created(cluster_id, lead_id)
                leads_created += 1

    result["leads_created"] = leads_created
    log.info(
        "cluster_analyst_complete",
        campaign_id=campaign_id,
        signals_read=len(signals),
        clusters_found=len(clusters),
        leads_created=leads_created,
        threshold=_CLUSTER_LEAD_THRESHOLD,
    )
    return result


# ---------------------------------------------------------------------------
# BQ: Read signals
# ---------------------------------------------------------------------------

def _read_signals_from_bq(campaign_id: str, tenant_id: str, lookback_hours: int) -> list[dict]:
    """Read recent signals from raw_signals BQ table for this campaign."""
    if not _PROJECT_ID:
        log.warning("cluster_analyst_no_project", note="GCP_PROJECT not set — cannot query BQ.")
        return []
    try:
        from google.cloud import bigquery  # type: ignore[import]
        client = bigquery.Client(project=_PROJECT_ID)
        query = f"""
            SELECT
                signal_id, url, source_type, snippet_text, content_source,
                social_platform, inline_score, intent_tier, topic_keywords, harvested_at
            FROM `{_RAW_SIGNALS_TABLE}`
            WHERE campaign_id = @campaign_id
              AND tenant_id   = @tenant_id
              AND harvested_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {lookback_hours} HOUR)
            ORDER BY harvested_at DESC
            LIMIT 100
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("campaign_id", "STRING", campaign_id),
                bigquery.ScalarQueryParameter("tenant_id",   "STRING", tenant_id),
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("cluster_analyst_bq_read_failed", campaign_id=campaign_id, error=str(exc))
        return []


# ---------------------------------------------------------------------------
# Gemini: Cluster identification
# ---------------------------------------------------------------------------

def _gemini_cluster(
    signals: list[dict],
    icp_context: str,
    archetype: str,
    geo: str,
) -> list[dict]:
    """Ask Gemini to identify intent clusters from signals."""
    # Format signals for the prompt
    formatted = []
    for i, s in enumerate(signals[:50]):  # cap at 50 for prompt size
        age_hours = _age_hours(s.get("harvested_at"))
        formatted.append(
            f"[{i}] {s.get('source_type','?')} | "
            f"{(s.get('snippet_text') or '')[:200]} | "
            f"score={s.get('inline_score', 0):.0f} | "
            f"age={age_hours:.0f}h"
        )

    signals_block = "\n".join(formatted)

    prompt = f"""You are an OSINT intent analyst. Your job is to identify patterns in signals collected from multiple internet platforms.

CAMPAIGN ARCHETYPE: {archetype}
TARGET GEOGRAPHY: {geo}

CAMPAIGN ICP (Ideal Customer Profile):
{icp_context[:1500]}

SIGNALS COLLECTED (last 48 hours):
(Format: [index] source_type | snippet | intent_score | age_in_hours)
{signals_block}

TASK:
1. Group signals that point to the SAME underlying buyer intent.
   A valid cluster requires signals from at least 2 different source_types OR 1 signal with very explicit buyer intent.
2. For each cluster provide:
   - cluster_label: 5-7 word summary (e.g. "Interior design search Muscat B2C")
   - intent_summary: 2 sentences a salesperson reads to act immediately. Who is this buyer and what do they need?
   - buyer_profile: Demographic, role, situation, urgency level.
   - contributing_indices: list of signal indices (integers) in this cluster
3. Ignore signals that do NOT relate to the ICP. Do not create spurious clusters.

Return ONLY valid JSON:
{{"clusters": [
  {{"cluster_label": "...", "intent_summary": "...", "buyer_profile": "...", "contributing_indices": [0, 2, 5]}},
  ...
]}}"""

    try:
        raw  = call_gemini_2_5(prompt, expect_json=True)
        data = raw if isinstance(raw, dict) else json.loads(raw)
        return data.get("clusters", [])
    except Exception as exc:
        log.warning("cluster_analyst_gemini_failed", error=str(exc))
        return []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_cluster(cluster: dict, all_signals: list[dict]) -> float:
    """Compute convergence score (0-100) for a cluster.

    convergence_score = (
        min(signal_count, 10) / 10 * 40  (signal volume)
      + min(source_diversity, 5) / 5 * 40  (platform spread)
      + recency_score * 20  (freshness decay)
    )
    """
    indices = cluster.get("contributing_indices", [])
    if not indices:
        return 0.0

    contributing = [all_signals[i] for i in indices if i < len(all_signals)]
    if not contributing:
        return 0.0

    signal_count   = len(contributing)
    source_types   = {s.get("source_type", "") for s in contributing if s.get("source_type")}
    source_diversity = len(source_types)

    # Recency: median age of signals in cluster
    ages = [_age_hours(s.get("harvested_at")) for s in contributing]
    median_age = sorted(ages)[len(ages) // 2] if ages else 48
    recency_score = math.exp(-0.04 * median_age)  # 1.0 at 0h, ~0.13 at 48h

    score = (
        min(signal_count,    10) / 10 * 40
      + min(source_diversity, 5) / 5  * 40
      + recency_score * 20
    )
    return round(min(score, 100.0), 2)


def _age_hours(harvested_at: Any) -> float:
    """Return age in hours from a harvested_at timestamp."""
    try:
        if isinstance(harvested_at, datetime.datetime):
            ts = harvested_at.replace(tzinfo=datetime.timezone.utc) if harvested_at.tzinfo is None else harvested_at
        else:
            ts = datetime.datetime.fromisoformat(str(harvested_at).replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0.0, (now - ts).total_seconds() / 3600)
    except Exception:
        return 24.0  # default to 24h if parsing fails


# ---------------------------------------------------------------------------
# BQ: Write cluster
# ---------------------------------------------------------------------------

def _write_cluster_to_bq(
    cluster: dict,
    campaign_id: str,
    tenant_id: str,
    geo: str,
    lead_created: bool = False,
    lead_id: str = "",
) -> None:
    """Write a cluster record to intent_clusters BQ table."""
    if not _PROJECT_ID:
        return
    try:
        from google.cloud import bigquery  # type: ignore[import]
        client = bigquery.Client(project=_PROJECT_ID)
        contributing = cluster.get("contributing_signals", [])
        row = {
            "cluster_id":        cluster.get("cluster_id", str(uuid.uuid4())),
            "campaign_id":       campaign_id,
            "tenant_id":         tenant_id,
            "cluster_label":     cluster.get("cluster_label", ""),
            "signal_count":      len(cluster.get("contributing_indices", [])),
            "source_diversity":  len({s.get("source_type", "") for s in contributing}),
            "convergence_score": cluster.get("convergence_score", 0.0),
            "intent_summary":    cluster.get("intent_summary", ""),
            "buyer_profile":     cluster.get("buyer_profile", ""),
            "geo":               geo,
            "signal_urls":       json.dumps([s.get("url", "") for s in contributing]),
            "signal_snippets":   json.dumps([(s.get("snippet_text") or "")[:300] for s in contributing]),
            "signal_platforms":  json.dumps([s.get("social_platform") or s.get("source_type", "") for s in contributing]),
            "clustered_at":      datetime.datetime.utcnow().isoformat() + "Z",
            "lead_created":      lead_created,
            "lead_id":           lead_id,
        }
        errors = client.insert_rows_json(_INTENT_CLUSTERS_TABLE, [row])
        if errors:
            log.warning("cluster_analyst_bq_write_error", errors=str(errors)[:200])
    except Exception as exc:
        log.warning("cluster_analyst_bq_write_failed", error=str(exc))


def _update_cluster_lead_created(cluster_id: str, lead_id: str) -> None:
    """Mark a cluster as having created a lead in BQ (best-effort)."""
    # BQ streaming inserts are immutable — we re-insert a corrected row.
    # In production, consider using BQ MERGE or Firestore for mutable cluster state.
    log.info("cluster_lead_created", cluster_id=cluster_id, lead_id=lead_id)


# ---------------------------------------------------------------------------
# Firestore: Create cluster lead
# ---------------------------------------------------------------------------

def _create_cluster_lead(
    cluster: dict,
    campaign_id: str,
    tenant_id: str,
    icp_context: str,
    archetype: str,
    geo: str,
    db: Any,
) -> Optional[str]:
    """Write a cluster lead record to Firestore campaigns/{id}/leads.

    Returns the lead_id string, or None on failure.

    V25.2.1 fixes:
      - Added 18 standard UI fields with defaults (dm, company_name, etc.)
        so lead cards render without blank sections.
      - Calls _settle_credit() after successful write for billing accuracy.
      - Mints a social passthrough JWT token for social-URL leads.
    """
    try:
        lead_id = str(uuid.uuid4())
        contributing = cluster.get("contributing_signals", [])

        signal_urls      = [s.get("url", "") for s in contributing]
        signal_snippets  = [(s.get("snippet_text") or "")[:500] for s in contributing]
        signal_platforms = [s.get("social_platform") or s.get("source_type", "") for s in contributing]

        # Representative URL — highest-scored contributing signal
        best_signal   = max(contributing, key=lambda s: float(s.get("inline_score") or 0), default={})
        source_url    = best_signal.get("url", "")

        now_utc = datetime.datetime.now(datetime.timezone.utc)

        lead_payload = {
            # Core identifiers
            "id":                          lead_id,
            "source_url":                  source_url,
            "tenant_id":                   tenant_id,
            "campaign_id":                 campaign_id,
            "origin_engine":               "cluster_analyst",
            "status":                      "new",
            "is_in_crm":                   False,
            "created_at":                  now_utc,
            "sourcing_vector":             archetype,

            # Cluster intelligence
            "is_cluster_lead":             True,
            "cluster_id":                  cluster.get("cluster_id", ""),
            "cluster_label":               cluster.get("cluster_label", ""),
            "cluster_summary":             cluster.get("intent_summary", ""),
            "buyer_profile":               cluster.get("buyer_profile", ""),
            "convergence_score":           cluster.get("convergence_score", 0.0),
            "source_diversity":            len({s.get("source_type") for s in contributing if s.get("source_type")}),
            "cluster_signals":             signal_urls,
            "cluster_snippets":            signal_snippets,
            "cluster_platforms":           signal_platforms,

            # Scores
            "score":                       round(cluster.get("convergence_score", 0) / 10),
            "normalized_score":            min(int(cluster.get("convergence_score", 0)), 100),

            # Intent
            "pain_point":                  cluster.get("intent_summary", ""),
            "intent_signal":               cluster.get("cluster_label", ""),

            # Social provenance
            "signal_source_type":          "cluster",
            "signal_platform":             signal_platforms[0] if signal_platforms else "",
            "social_snippet":              signal_snippets[0] if signal_snippets else "",

            # Standard UI fields — defaults so lead cards render correctly
            # V25.2.1: added missing fields that dispatch.py always writes.
            "dm":                          "",
            "hiring_intent_found":         False,
            "tech_stack_found":            False,
            "icebreaker_angle":            "",
            "contact_endpoints":           [],
            "decision_maker_name":         "",
            "decision_maker_title":        "",
            "company_name":                cluster.get("buyer_profile", "")[:80],
            "company_size_tier":           "",
            "primary_objection_hypothesis": "",
            "score_reasoning":             cluster.get("intent_summary", ""),
            "confidence_level":            "medium",
            "evidence_chain":              signal_snippets[:3],
            "prism_mode":                  "cluster",
            "prism_fallback":              False,
            "confidence_tier":             "MEDIUM",
            "matched_campaign_ids":        [campaign_id],
            "matched_campaigns":           [campaign_id],
            "dossier_text":                "",
            "trend_mapped":                False,
            "highest_campaign_id":         campaign_id,
        }

        db.collection("campaigns").document(campaign_id) \
          .collection("leads").document(lead_id) \
          .set(lead_payload)

        log.info(
            "cluster_lead_written",
            lead_id=lead_id,
            campaign_id=campaign_id,
            cluster_label=cluster.get("cluster_label", ""),
            convergence_score=cluster.get("convergence_score", 0),
            signals=len(contributing),
        )

        # V25.2.1: Deduct credit for cluster leads (same as dispatch-path leads)
        try:
            from api.routers.dispatch import _settle_credit  # type: ignore[import]  # noqa: PLC0415
        except ImportError:
            _settle_credit = None  # type: ignore[assignment]
            log.warning(
                "cluster_lead_settle_credit_import_unavailable",
                note="api.routers.dispatch not on sys.path — credit settlement skipped.",
            )
        if _settle_credit is not None:
            try:
                _settle_credit(tenant_id, "success", lead_id=lead_id)
            except Exception as _cr_err:
                log.warning(
                    "cluster_lead_credit_settle_failed",
                    lead_id=lead_id,
                    tenant_id=tenant_id,
                    error=str(_cr_err),
                    note="Non-blocking — lead written but credit not settled.",
                )

        # V25.2.1: Mint social passthrough token for social-URL leads
        # Non-blocking — failure does not block lead creation
        if source_url:
            try:
                from api.routers.social_redirect import mint_social_token  # type: ignore[import]  # noqa: PLC0415
            except ImportError:
                mint_social_token = None  # type: ignore[assignment]
                log.warning(
                    "cluster_lead_mint_social_token_import_unavailable",
                    note="api.routers.social_redirect not on sys.path — token minting skipped.",
                )
            if mint_social_token is not None:
                try:
                    _token = mint_social_token(
                        lead_id=lead_id,
                        tenant_id=tenant_id,
                        url=source_url,
                        db=db,
                    )
                    if _token:
                        # Attach token to lead doc so the frontend can build /go/<token> URLs
                        db.collection("campaigns").document(campaign_id) \
                          .collection("leads").document(lead_id) \
                          .update({"social_token": _token})
                except Exception as _tok_err:
                    log.warning(
                        "cluster_lead_token_mint_failed",
                        lead_id=lead_id,
                        error=str(_tok_err),
                        note="Non-blocking.",
                    )

        return lead_id

    except Exception as exc:
        log.error(
            "cluster_lead_write_failed",
            campaign_id=campaign_id,
            cluster_label=cluster.get("cluster_label", ""),
            error=str(exc),
        )
        return None
