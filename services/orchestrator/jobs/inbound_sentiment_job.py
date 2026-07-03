"""
Orchestrator — Inbound Sentiment Job.

Scheduled job (Cloud Scheduler — every 6 hours) that:
  1. Finds all tenants with inbound_radar.enabled == True
  2. For each active campaign, runs InboundSentimentService
  3. Writes high-intent signals to Firestore: inbound_signals/{uid}_{signal_id}
  4. Boosts matching BigQuery Intent_Keywords (RLHF yield_weight)
  5. Updates users/{uid}.inbound_radar stats

Entry points:
  - run()             — called by the Flask trigger route (daemon thread)
  - __main__ block    — for local testing: python -m jobs.inbound_sentiment_job

V23.5 — added 2026-06-08
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

# Allow running as __main__ from the orchestrator root
_HERE = os.path.dirname(os.path.abspath(__file__))
_ORCH_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, _ORCH_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from google.cloud import firestore  # noqa: E402 — after sys.path bootstrap

from core.clients import get_db, get_bq_client  # type: ignore[import]
from core.config import PROJECT_ID  # type: ignore[import]
from core.logging import get_logger  # type: ignore[import]
from services.inbound_sentiment_service import InboundSentimentService  # type: ignore[import]

log = get_logger("orchestrator.inbound_sentiment_job")

BQ_DATASET  = os.environ.get("BQ_DATASET", "leads_intelligence")
BQ_KW_TABLE = f"{PROJECT_ID}.{BQ_DATASET}.Intent_Keywords"

MIN_INTENT_SCORE = 0.45   # Minimum score to write a signal to Firestore (V24.1.25 — lowered from 0.55)
MAX_SIGNALS_PER_TENANT = 25  # Serper quota guard per run
RLHF_BOOST  = 0.12        # yield_weight increment for hot keywords
RLHF_MIN    = 0.70        # Only boost keywords from signals above this score

# V25.2.2: Raised from 5 → 20 (4× raw URL pool, same Serper credit cost per call)
# and from 8 → 14 queries (freed capacity from cross-run dedup cache)
_RESULTS_PER_QUERY = int(os.environ.get("INBOUND_RESULTS_PER_QUERY", "20"))
_MAX_QUERIES       = int(os.environ.get("INBOUND_MAX_QUERIES", "14"))

# V25.2.2: Cross-run URL dedup — 7-day window prevents re-scoring same URLs every 6h.
_DEDUP_TTL_DAYS = 7
_DEDUP_COLL     = "inbound_dedup"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run() -> dict:
    """
    Run the inbound sentiment job for all eligible tenants.

    Returns:
        {"tenants_processed": int, "signals_written": int, "errors": int}
    """
    db  = get_db()
    bq  = get_bq_client()

    tenants_processed = 0
    signals_written   = 0
    errors            = 0

    # Stream all tenants with inbound_radar enabled
    try:
        tenant_docs = (
            db.collection("users")
            .where(filter=firestore.FieldFilter("inbound_radar.enabled", "==", True))
            .stream()
        )
    except Exception:
        # Fallback if the composite index isn't ready yet — scan all users
        log.warning("inbound_job_index_missing_fallback")
        tenant_docs = db.collection("users").stream()

    for snap in tenant_docs:
        uid      = snap.id
        user_doc = snap.to_dict() or {}

        # Skip if radar explicitly disabled
        radar_cfg = user_doc.get("inbound_radar") or {}
        if radar_cfg.get("enabled") is False:
            continue

        try:
            tenant_signals = _run_for_tenant(db, bq, uid, user_doc)
            signals_written   += len(tenant_signals)
            tenants_processed += 1

            # Update radar stats on the user doc
            top_kws = _top_keywords(tenant_signals)
            db.collection("users").document(uid).set(
                {
                    "inbound_radar": {
                        "enabled":          True,
                        "last_ran_at":      firestore.SERVER_TIMESTAMP,
                        "signals_this_week": len(tenant_signals),
                        "top_pain_keywords": top_kws,
                    }
                },
                merge=True,
            )

        except Exception as exc:
            errors += 1
            log.error("inbound_job_tenant_failed", uid=uid[:8], error=str(exc))

    result = {
        "tenants_processed": tenants_processed,
        "signals_written":   signals_written,
        "errors":            errors,
    }
    log.info("inbound_sentiment_job_complete", **result)
    return result


# ---------------------------------------------------------------------------
# Per-tenant pipeline
# ---------------------------------------------------------------------------

def _run_for_tenant(db, bq, uid: str, user_doc: dict) -> list[dict]:
    """Run signal detection for a single tenant. Returns written signals."""
    # Fetch this tenant's active campaigns (limit 5 — don't over-burn Serper quota)
    # Canonical field is tenant_id (used by cron/sweep and all other paths).
    # Fallback to uid for backward compatibility with legacy campaign docs.
    campaigns = [
        {**c.to_dict(), "campaign_id": c.id}
        for c in db.collection("campaigns")
        .where(filter=firestore.FieldFilter("tenant_id", "==", uid))
        .where(filter=firestore.FieldFilter("status", "==", "active"))
        .limit(5)
        .stream()
    ]
    if not campaigns:
        # Fallback: try legacy 'uid' field
        campaigns = [
            {**c.to_dict(), "campaign_id": c.id}
            for c in db.collection("campaigns")
            .where(filter=firestore.FieldFilter("uid", "==", uid))
            .where(filter=firestore.FieldFilter("status", "==", "active"))
            .limit(5)
            .stream()
        ]

    if not campaigns:
        log.info("inbound_job_no_active_campaigns", uid=uid[:8])
        return []

    all_signals: list[dict] = []

    for camp in campaigns:
        # Force a hard reset of these variables at the very top of the loop block
        current_pain_points = camp.get("pain_points", [])
        current_target_audience = camp.get("target_audience", [])
        if not current_pain_points:
            current_pain_points = []
        if not current_target_audience:
            current_target_audience = []
        persona = {}

        persona_id = camp.get("persona_id")
        if persona_id:
            persona_snap = (
                db.collection("tenant_profiles")
                .document(uid)
                .collection("personas")
                .document(persona_id)
                .get()
            )
            if not persona_snap.exists:
                # Persona was deleted after campaign creation — fall through to bio fallback
                log.warning(
                    "inbound_job_persona_missing",
                    uid=uid[:8],
                    persona_id=persona_id,
                    note="Persona doc not found — falling back to campaign bio.",
                )
                persona_id = None
            else:
                persona = persona_snap.to_dict() or {}
                if not (persona.get("uid") == uid or persona.get("tenant_id") == uid):
                    log.warning(
                        "invalid_persona_data",
                        uid=uid[:8],
                        persona_id=persona_id,
                        reason="Persona multi-tenancy violation detected.",
                    )
                    continue  # Hard skip on ownership violation — security boundary

        if not persona_id:
            # V24.5.1: Fall back to campaign bio + keywords instead of silently skipping.
            # Campaigns created before the Persona Vault feature have no persona_id.
            # Skipping them meant the Inbound Radar produced zero signals for all pre-Vault tenants.
            camp_bio = camp.get("persona_bio") or camp.get("bio") or ""
            camp_kws = camp.get("persona_keywords") or camp.get("keywords") or ""
            camp_name = camp.get("persona_name") or camp.get("name") or "Default"
            if not camp_bio:
                log.info(
                    "inbound_job_no_bio_skip",
                    uid=uid[:8],
                    campaign_id=camp["campaign_id"],
                    note="No bio or persona — skipping campaign.",
                )
                continue
            # Synthetic persona from campaign fields
            persona = {
                "uid":      uid,
                "tenant_id": uid,
                "name":     camp_name,
                "bio":      camp_bio,
                "keywords": camp_kws,
                "is_legacy": True,
            }
            log.info(
                "inbound_job_persona_fallback",
                uid=uid[:8],
                campaign_id=camp["campaign_id"],
                note="No persona_id — using campaign bio as synthetic persona.",
            )

        try:
            # 1. Run web and social inbound sentiment service
            svc     = InboundSentimentService(persona=persona, campaign=camp)
            # V25.2.2: Load cross-run seen hashes to skip already-scored URLs
            seen_hashes = _load_dedup_hashes(db, uid)
            signals = svc.run(
                max_queries=_MAX_QUERIES,
                results_per_query=_RESULTS_PER_QUERY,
                seen_url_hashes=seen_hashes,
            )
            
            # 2. Run Google Maps competitor review intelligence service
            try:
                from services.inbound_maps_service import InboundMapsService
                maps_svc = InboundMapsService(persona=persona, campaign=camp)
                maps_signals = maps_svc.run(max_places=5)
                signals.extend(maps_signals)
            except Exception as maps_exc:
                log.warning(
                    "inbound_maps_svc_failed",
                    uid=uid[:8],
                    campaign_id=camp["campaign_id"],
                    error=str(maps_exc),
                )

            high    = [s for s in signals if s["intent_score"] >= MIN_INTENT_SCORE]
            all_signals.extend(high[:MAX_SIGNALS_PER_TENANT])

            log.info(
                "inbound_signals_found",
                uid=uid[:8],
                campaign=camp.get("name", camp["campaign_id"])[:30],
                total=len(signals),
                high_intent=len(high),
            )
        except Exception as exc:
            log.warning(
                "inbound_svc_failed",
                uid=uid[:8],
                campaign_id=camp["campaign_id"],
                error=str(exc),
            )

    if not all_signals:
        return []

    # Deduplicate across campaigns (same URL from two campaigns → keep higher score)
    unique: dict[str, dict] = {}
    for sig in all_signals:
        sid = sig["signal_id"]
        if sid not in unique or sig["intent_score"] > unique[sid]["intent_score"]:
            unique[sid] = sig
    deduped = sorted(unique.values(), key=lambda x: x["intent_score"], reverse=True)

    # Write to Firestore (idempotent via signal_id — set with merge=True)
    _write_signals(db, uid, deduped)

    # V25.2.2: Save processed URL hashes to Firestore dedup cache for next run
    _save_dedup_hashes(db, uid, [s.get("source_url", "") for s in deduped])

    # RLHF: boost keywords from ACTIVE_SEEKING / COMPETITOR_CHURN signals
    _boost_rlhf(bq, uid, deduped)

    return deduped


# ---------------------------------------------------------------------------
# Firestore write
# ---------------------------------------------------------------------------

def _write_signals(db, uid: str, signals: list[dict]) -> None:
    """Batch-upsert signals into inbound_signals collection."""
    if not signals:
        return

    import datetime

    # V25.2.2: Signals expire after 30 days (Firestore TTL field)
    _signal_expire = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
    )

    # Firestore batch limit is 500 — chunk if needed
    CHUNK = 400
    for i in range(0, len(signals), CHUNK):
        batch = db.batch()
        for sig in signals[i : i + CHUNK]:
            doc_id = f"{uid}_{sig['signal_id']}"
            ref    = db.collection("inbound_signals").document(doc_id)
            batch.set(
                ref,
                {
                    "tenant_id":           uid,
                    "signal_id":           sig["signal_id"],
                    "source_url":          sig.get("source_url", ""),
                    "source_platform":     sig.get("source_platform", "web"),
                    "headline":            sig.get("headline", ""),
                    "snippet":             sig.get("snippet", ""),
                    "serper_query":        sig.get("serper_query", ""),
                    "triggering_keyword":  sig.get("triggering_keyword", ""),
                    "intent_label":        sig.get("intent_label", "TREND"),
                    "intent_score":        sig.get("intent_score", 0.0),
                    "pain_keywords":       sig.get("pain_keywords", []),
                    "company_name":        sig.get("company_name"),
                    "industry_hint":       sig.get("industry_hint"),
                    "gemini_reasoning":    sig.get("gemini_reasoning", ""),
                    "matched_persona":     sig.get("matched_persona", ""),
                    "matched_campaign_id": sig.get("matched_campaign_id", ""),
                    "week":                sig.get("week", ""),
                    "status":              sig.get("status", "new"),
                    "synced_at":           firestore.SERVER_TIMESTAMP,
                    "expire_at":           _signal_expire,  # V25.2.2: TTL field
                },
                merge=True,
            )
        batch.commit()
        log.info("inbound_signals_batch_written", uid=uid[:8], count=min(CHUNK, len(signals) - i))


# ---------------------------------------------------------------------------
# RLHF boost
# ---------------------------------------------------------------------------

def _boost_rlhf(bq, uid: str, signals: list[dict]) -> None:
    """
    Boost yield_weight in BigQuery Intent_Keywords for keywords that appear
    in high-intent signals (score >= RLHF_MIN).

    This closes the feedback loop: the pipeline generates smarter search
    queries for topics that are resonating as inbound signals.
    """
    hot = [s for s in signals if s.get("intent_score", 0) >= RLHF_MIN]
    if not hot:
        return

    kw_set: set[str] = set()
    for sig in hot:
        for kw in sig.get("pain_keywords", []):
            if kw and len(kw) >= 3:
                kw_set.add(kw.lower().strip())

    if not kw_set:
        return

    kw_list = ", ".join(f"'{kw}'" for kw in sorted(kw_set)[:10])
    sql = f"""
        UPDATE `{BQ_KW_TABLE}`
        SET   yield_weight = LEAST(1.0, yield_weight + {RLHF_BOOST}),
              updated_at   = CURRENT_TIMESTAMP()
        WHERE tenant_id = @tenant_id
          AND LOWER(keyword) IN ({kw_list})
    """
    job_config = bq.__class__  # avoid import at module level
    try:
        from google.cloud import bigquery as _bq
        cfg = _bq.QueryJobConfig(
            query_parameters=[
                _bq.ScalarQueryParameter("tenant_id", "STRING", uid)
            ]
        )
        bq.query(sql, job_config=cfg).result()
        log.info(
            "rlhf_boost_applied",
            uid=uid[:8],
            keywords=sorted(kw_set)[:5],
            boost=RLHF_BOOST,
        )
    except Exception as exc:
        log.warning("rlhf_boost_failed", uid=uid[:8], error=str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _top_keywords(signals: list[dict]) -> list[str]:
    """Return the 5 most frequent pain_keywords across all signals."""
    kws: list[str] = []
    for sig in signals:
        kws.extend(sig.get("pain_keywords") or [])
    return [kw for kw, _ in Counter(kws).most_common(5)]


# ---------------------------------------------------------------------------
# Cross-run URL dedup helpers (V25.2.2)
# ---------------------------------------------------------------------------

def _url_hash(url: str) -> str:
    """Stable 16-char SHA-256 hash of a URL for dedup comparison."""
    import hashlib
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _load_dedup_hashes(db, uid: str) -> set[str]:
    """Load the set of URL hashes seen in the last 7 days for this tenant.

    Reads from inbound_dedup/{uid} which stores a map of hash -> expires_at.
    Returns a frozenset of hash strings. Non-blocking: returns empty set on error.
    """
    import datetime
    try:
        doc = db.collection(_DEDUP_COLL).document(uid).get()
        if not doc.exists:
            return set()
        data = doc.to_dict() or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        # Expire entries older than TTL and collect valid hashes
        valid = set()
        for h, exp in data.items():
            if h.startswith("_"):  # skip metadata fields
                continue
            try:
                if hasattr(exp, "tzinfo"):
                    exp_ts = exp if exp.tzinfo else exp.replace(tzinfo=datetime.timezone.utc)
                else:
                    exp_ts = datetime.datetime.fromisoformat(str(exp)).replace(
                        tzinfo=datetime.timezone.utc
                    )
                if exp_ts > now:
                    valid.add(h)
            except Exception:
                pass
        log.info("inbound_dedup_loaded", uid=uid[:8], cached_hashes=len(valid))
        return valid
    except Exception as exc:
        log.warning("inbound_dedup_load_failed", uid=uid[:8], error=str(exc),
                    note="Non-blocking — will re-score all URLs this run.")
        return set()


def _save_dedup_hashes(db, uid: str, urls: list[str]) -> None:
    """Persist URL hashes to inbound_dedup/{uid} with 7-day expiry.

    Uses Firestore merge=True to accumulate hashes across runs.
    Each field is hash_string -> expires_at ISO timestamp.
    Non-blocking: failures only log.
    """
    import datetime
    if not urls:
        return
    try:
        expires = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=_DEDUP_TTL_DAYS)
        ).isoformat()
        payload = {_url_hash(u): expires for u in urls if u}
        if not payload:
            return
        db.collection(_DEDUP_COLL).document(uid).set(payload, merge=True)
        log.info("inbound_dedup_saved", uid=uid[:8], new_hashes=len(payload))
    except Exception as exc:
        log.warning("inbound_dedup_save_failed", uid=uid[:8], error=str(exc),
                    note="Non-blocking — next run may re-score some URLs.")


# ---------------------------------------------------------------------------
# Local test entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
