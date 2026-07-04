"""
Signal BQ Writer — V25.2.0
===========================
Writes every scored signal from signal_harvest to the
swarm_analytics.raw_signals BigQuery table.

DESIGN:
  - Non-blocking: runs in a daemon thread — zero latency impact on harvest
  - Writes ALL signals (HIGH, MEDIUM, LOW) — BQ is the full signal history
  - Firestore (Stage 7) still only receives HIGH/MEDIUM — this is additive
  - Provides the raw data for signal_cluster_analyst.py

Table schema: swarm_analytics.raw_signals
  See implementation_plan.md for full DDL.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import uuid
from typing import Any

from core.logging import get_logger                    # type: ignore[import]
from services.signal_sources.base import SignalItem    # type: ignore[import]

log = get_logger("pipeline.signal_bq_writer")

_PROJECT_ID = os.environ.get("GCP_PROJECT", os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
_TABLE_REF  = f"{_PROJECT_ID}.swarm_analytics.raw_signals"


def write_signals_to_bq(
    scored_results: list[tuple[SignalItem, dict]],
    campaign: dict,
) -> None:
    """Non-blocking write of scored signals to BQ raw_signals table.

    Spawns a daemon thread to perform the BQ streaming insert. Returns
    immediately — does not block the harvest pipeline.

    Args:
        scored_results: list of (SignalItem, score_dict) from inline scoring.
                        score_dict has keys: tier (HIGH/MEDIUM/LOW), score (float),
                        topic_keywords (list).
        campaign:       Full campaign dict (for campaign_id, tenant_id, archetype).
    """
    if not scored_results:
        return
    if not _PROJECT_ID:
        log.warning("signal_bq_writer_no_project", note="GCP_PROJECT env var not set — BQ write skipped.")
        return

    t = threading.Thread(
        target=_write_batch,
        args=(scored_results, campaign),
        daemon=True,
        name="signal_bq_writer",
    )
    t.start()


def _write_batch(scored_results: list[tuple[SignalItem, dict]], campaign: dict) -> None:
    """Blocking BQ streaming insert (runs in daemon thread)."""
    try:
        from google.cloud import bigquery  # type: ignore[import]
        client = bigquery.Client(project=_PROJECT_ID)

        campaign_id = campaign.get("id") or campaign.get("campaign_id", "")
        tenant_id   = campaign.get("tenant_id", "")
        archetype   = campaign.get("sourcing_vector", "B2B")
        geo         = campaign.get("location", "")
        now_iso     = datetime.datetime.utcnow().isoformat() + "Z"

        rows: list[dict] = []
        for signal, score_dict in scored_results:
            rows.append({
                "signal_id":       str(uuid.uuid4()),
                "campaign_id":     campaign_id,
                "tenant_id":       tenant_id,
                "url":             (signal.url or "")[:500],
                "source_type":     signal.source_type or "",
                "snippet_text":    (signal.text or "")[:2000],
                "content_source":  signal.metadata.get("content_source", "unknown"),
                "social_platform": signal.metadata.get("social_platform", ""),
                "inline_score":    float(score_dict.get("score", 0) if isinstance(score_dict, dict) else 0),
                "intent_tier":     score_dict.get("tier", "LOW") if isinstance(score_dict, dict) else "LOW",
                "geo":             geo,
                "topic_keywords":  json.dumps(score_dict.get("topic_keywords", []) if isinstance(score_dict, dict) else []),
                "harvested_at":    now_iso,
                "archetype":       archetype,
            })

        errors = client.insert_rows_json(_TABLE_REF, rows)
        if errors:
            log.warning(
                "signal_bq_writer_insert_errors",
                table=_TABLE_REF,
                error_count=len(errors),
                first_error=str(errors[0])[:200],
            )
            # P2-EXT-3: Retry failed rows once.
            # BQ insert_rows_json returns a list of dicts; each dict has an
            # 'index' key indicating which row failed and an 'errors' key
            # with the failure details.
            failed_indices: set[int] = set()
            for err_entry in errors:
                if isinstance(err_entry, dict) and "index" in err_entry:
                    failed_indices.add(err_entry["index"])
            if failed_indices:
                retry_rows = [rows[i] for i in sorted(failed_indices) if i < len(rows)]
                if retry_rows:
                    log.info(
                        "signal_bq_writer_retrying",
                        retry_count=len(retry_rows),
                        table=_TABLE_REF,
                    )
                    retry_errors = client.insert_rows_json(_TABLE_REF, retry_rows)
                    if retry_errors:
                        log.warning(
                            "signal_bq_writer_retry_failed",
                            table=_TABLE_REF,
                            retry_error_count=len(retry_errors),
                            first_retry_error=str(retry_errors[0])[:200],
                        )
                    else:
                        log.info(
                            "signal_bq_writer_retry_success",
                            recovered_rows=len(retry_rows),
                            table=_TABLE_REF,
                        )
        else:
            log.info(
                "signal_bq_writer_complete",
                table=_TABLE_REF,
                rows_written=len(rows),
                campaign_id=campaign_id,
            )
    except Exception as exc:
        log.warning(
            "signal_bq_writer_failed",
            error=str(exc),
            note="Non-critical — harvest continues normally.",
        )
