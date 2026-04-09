"""
Shadow Learner Aggregator — Cloud Run Job
Runs every 12 hours via Cloud Scheduler.

Pipeline:
  1. Query BigQuery rlhf_events for the last 12h of conversion signals
  2. Aggregate: top intent_hash patterns ranked by net conversion weight
  3. Compress into global_swarm_weights JSON object
  4. Write to Firestore: system_config/global_swarm_weights
"""

import os
import json
import datetime
import logging

from google.cloud import bigquery
from google.cloud import firestore

logging.basicConfig(level=logging.INFO, format="[SHADOW-LEARNER] %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

PROJECT_ID  = os.environ.get("PROJECT_ID",  "sideio-leads-v16")
BQ_DATASET  = os.environ.get("BQ_DATASET",  "swarm_analytics")
BQ_TABLE    = os.environ.get("BQ_TABLE",    "rlhf_events")
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "12"))

# ── BigQuery aggregation SQL ──────────────────────────────────────────────────
# Computes net_conversions (converted wins minus ignored penalties) per intent_hash
# and per prism_mode bucket over the rolling WINDOW_HOURS window.
# Minimum 3 occurrences to filter noise.
AGGREGATION_SQL = """
SELECT
    intent_hash,
    prism_mode,
    COUNTIF(conversion_status IN ('converted', 'won'))                     AS conversions,
    COUNTIF(conversion_status IN ('ignored',   'lost'))                    AS rejections,
    COUNTIF(conversion_status IN ('contacted', 'replied'))                 AS contacts,
    COUNT(*)                                                               AS total_events,
    ROUND(
        SAFE_DIVIDE(
            COUNTIF(conversion_status IN ('converted', 'won'))             
            - (COUNTIF(conversion_status IN ('ignored', 'lost')) * 0.33),
            COUNT(*)
        ), 4
    )                                                                      AS net_weight
FROM `{project}.{dataset}.{table}`
WHERE
    timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {window} HOUR)
    AND intent_hash IS NOT NULL
    AND intent_hash != ''
GROUP BY
    intent_hash, prism_mode
HAVING
    total_events >= 3
ORDER BY
    net_weight DESC
LIMIT 500
""".format(
    project=PROJECT_ID,
    dataset=BQ_DATASET,
    table=BQ_TABLE,
    window=WINDOW_HOURS,
)


def run_aggregation() -> dict:
    """Execute BigQuery aggregation and return structured results."""
    bq = bigquery.Client(project=PROJECT_ID)
    log.info(f"Running aggregation query over last {WINDOW_HOURS}h window...")

    job = bq.query(AGGREGATION_SQL)
    rows = list(job.result())

    log.info(f"Query returned {len(rows)} intent_hash patterns")

    # Build the swarm weights payload
    weights_by_mode = {}
    top_hashes = []

    for row in rows:
        intent_hash   = row["intent_hash"]
        prism_mode    = row["prism_mode"] or "GeneralDomain"
        net_weight    = float(row["net_weight"] or 0)
        conversions   = int(row["conversions"])
        total_events  = int(row["total_events"])

        # Per-mode bucket
        if prism_mode not in weights_by_mode:
            weights_by_mode[prism_mode] = []
        weights_by_mode[prism_mode].append({
            "hash":        intent_hash,
            "net_weight":  net_weight,
            "conversions": conversions,
            "total":       total_events,
        })

        # Global top-50 list (already sorted DESC by net_weight)
        if len(top_hashes) < 50:
            top_hashes.append({
                "hash":       intent_hash,
                "mode":       prism_mode,
                "net_weight": net_weight,
                "support":    total_events,
            })

    return {
        "generated_at":    datetime.datetime.utcnow().isoformat() + "Z",
        "window_hours":    WINDOW_HOURS,
        "total_patterns":  len(rows),
        "top_hashes":      top_hashes,
        "weights_by_mode": {
            mode: sorted(entries, key=lambda x: x["net_weight"], reverse=True)[:100]
            for mode, entries in weights_by_mode.items()
        },
    }


def write_to_firestore(payload: dict):
    """Write aggregated weights to Firestore system_config/global_swarm_weights."""
    db = firestore.Client(project=PROJECT_ID)
    ref = db.collection("system_config").document("global_swarm_weights")
    ref.set(payload, merge=False)  # Full overwrite — this is a time-series snapshot
    log.info("Wrote global_swarm_weights to Firestore system_config/global_swarm_weights")


def main():
    log.info(f"Shadow Learner Aggregator starting | project={PROJECT_ID} | window={WINDOW_HOURS}h")

    try:
        payload = run_aggregation()
    except Exception as e:
        log.error(f"BigQuery aggregation failed: {e}")
        raise SystemExit(1)

    try:
        write_to_firestore(payload)
    except Exception as e:
        log.error(f"Firestore write failed: {e}")
        raise SystemExit(1)

    log.info(
        f"Shadow Learner cycle complete | "
        f"patterns={payload['total_patterns']} | "
        f"top_hashes={len(payload['top_hashes'])}"
    )


if __name__ == "__main__":
    main()
