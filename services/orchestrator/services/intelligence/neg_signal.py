"""
Orchestrator — Negative Signal Graph intelligence service.

Extracted from the monolithic main.py negative signal block.

Responsibilities:
  1. ``_do_neg_signal_insert()``   — Synchronous BQ streaming insert.
  2. ``async_neg_signal_insert()`` — Fire-and-forget daemon thread wrapper.

Design contract (V22 TSD §25.2.2):
  * Threads MUST be ``daemon=True``.
  * Any BQ failure MUST be logged at WARNING and swallowed.
  * The HTTP 200 on lead rejection MUST never wait for BigQuery.
"""
from __future__ import annotations

import datetime
import threading

from core.logging import get_logger

log = get_logger(__name__)

# Rejection reasons that trigger a Negative_Signals insert
# V24.5 (L8-4): Expanded rejection reasons that trigger BQ domain suppression.
# Previously only "competitor" and "author" recorded to swarm_analytics.Negative_Signals.
# Domains rejected as wrong_industry or not_icp also re-appear in every produce cycle;
# they must be suppressed in the neg shield to prevent repeated wasteful scoring.
NEG_SIGNAL_REASONS: frozenset[str] = frozenset({
    "competitor",    # Competing service offering
    "author",        # Content author / influencer, not a buyer
    "wrong_industry", # Domain clearly outside the target industry
    "not_icp",       # Domain does not match ideal customer profile
    "low_quality",   # Consistently low-score domain
})


def _do_neg_signal_insert(
    entity_name: str,
    root_domain: str,
    rejection_reason: str,
    tenant_id: str,
    project_id: str,
) -> None:
    """Synchronous BQ streaming insert into swarm_analytics.Negative_Signals.

    Runs inside a daemon thread only — never called synchronously on the
    request path.  All failures are logged at WARNING and swallowed.

    Args:
        entity_name:      Company name or entity label.
        root_domain:      Cleaned root domain (e.g. ``"salesforce.com"``).
        rejection_reason: ``"Competitor"`` or ``"Author"``.
        tenant_id:        Tenant UID (or ``"GLOBAL"`` for L0 overrides).
        project_id:       GCP project ID.
    """
    try:
        from google.cloud import bigquery as _bq_lib
        bq = _bq_lib.Client(project=project_id)
        table_ref = f"{project_id}.swarm_analytics.Negative_Signals"
        row = {
            "entity_name":      entity_name,
            "root_domain":      root_domain,
            "rejection_reason": rejection_reason,
            "tenant_id":        tenant_id,
            "timestamp":        datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        errors = bq.insert_rows_json(table_ref, [row])
        if errors:
            log.warning(
                "neg_signal_bq_streaming_error",
                errors=str(errors),
                domain=root_domain,
                tenant=tenant_id[:8],
            )
        else:
            log.info(
                "neg_signal_inserted",
                reason=rejection_reason,
                domain=root_domain,
                tenant=tenant_id[:8],
            )
    except Exception as exc:
        log.warning(
            "neg_signal_insert_failed",
            error=str(exc),
            domain=root_domain,
            tenant=tenant_id[:8],
        )


def async_neg_signal_insert(
    entity_name: str,
    root_domain: str,
    rejection_reason: str,
    tenant_id: str,
    project_id: str,
) -> None:
    """Fire-and-forget wrapper — spawns daemon thread, never raises.

    Args:
        entity_name:      Company name.
        root_domain:      Root domain string.
        rejection_reason: ``"Competitor"`` or ``"Author"``.
        tenant_id:        Tenant UID.
        project_id:       GCP project ID.
    """
    try:
        t = threading.Thread(
            target=_do_neg_signal_insert,
            args=(entity_name, root_domain, rejection_reason, tenant_id, project_id),
            daemon=True,
        )
        t.start()
    except Exception as exc:
        log.warning("neg_signal_thread_spawn_failed", error=str(exc))
