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
from typing import Optional

from core.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# INT-02: Neg signal TTL — 90-day expiry on all BQ rows.
# ---------------------------------------------------------------------------
_NEG_SIGNAL_TTL_DAYS: int = 90

# ---------------------------------------------------------------------------
# INT-06: Shared platforms — suppressing root_domain is meaningless because
# millions of users share the same domain.  Store the full URL path instead.
# ---------------------------------------------------------------------------
_SHARED_PLATFORMS: frozenset[str] = frozenset({
    "reddit.com", "medium.com", "quora.com", "linkedin.com",
    "facebook.com", "twitter.com", "x.com", "youtube.com",
    "instagram.com", "tiktok.com", "pinterest.com", "tumblr.com",
    "wordpress.com", "blogspot.com", "substack.com",
    "github.com", "news.ycombinator.com",
})

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


def _resolve_suppression_key(root_domain: str, source_url: Optional[str] = None) -> str:
    """Return the correct suppression key for a domain.

    INT-06: For shared platforms (reddit.com, medium.com, etc.), suppress
    the full URL path instead of the useless root domain.  For normal
    business domains, return the root_domain as-is.
    """
    cleaned = (root_domain or "").lower().strip()
    if cleaned in _SHARED_PLATFORMS and source_url:
        # Use full URL path up to query params for granular suppression
        from urllib.parse import urlparse
        parsed = urlparse(source_url)
        path_key = f"{parsed.netloc}{parsed.path}".rstrip("/").lower()
        if path_key:
            return path_key
    return cleaned


def _do_neg_signal_insert(
    entity_name: str,
    root_domain: str,
    rejection_reason: str,
    tenant_id: str,
    project_id: str,
    source_url: Optional[str] = None,
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
        source_url:       Original source URL (used for shared-platform path resolution).
    """
    try:
        from google.cloud import bigquery as _bq_lib
        bq = _bq_lib.Client(project=project_id)
        table_ref = f"{project_id}.swarm_analytics.Negative_Signals"

        # INT-06: Resolve suppression key (full URL path for shared platforms)
        suppression_key = _resolve_suppression_key(root_domain, source_url)

        # INT-02: Compute 90-day expiry timestamp
        now_utc = datetime.datetime.utcnow()
        expires_at = now_utc + datetime.timedelta(days=_NEG_SIGNAL_TTL_DAYS)

        row = {
            "entity_name":      entity_name,
            "root_domain":      suppression_key,
            "rejection_reason": rejection_reason,
            "tenant_id":        tenant_id,
            "timestamp":        now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at":       expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    source_url: Optional[str] = None,
) -> None:
    """Fire-and-forget wrapper — spawns daemon thread, never raises.

    Args:
        entity_name:      Company name.
        root_domain:      Root domain string.
        rejection_reason: ``"Competitor"`` or ``"Author"``.
        tenant_id:        Tenant UID.
        project_id:       GCP project ID.
        source_url:       Original source URL (for shared-platform path resolution).
    """
    try:
        t = threading.Thread(
            target=_do_neg_signal_insert,
            args=(entity_name, root_domain, rejection_reason, tenant_id, project_id, source_url),
            daemon=True,
        )
        t.start()
    except Exception as exc:
        log.warning("neg_signal_thread_spawn_failed", error=str(exc))


# ---------------------------------------------------------------------------
# INT-02: Unsuppression + Suppression listing
# ---------------------------------------------------------------------------

def unsuppress_domain(
    tenant_id: str,
    domain: str,
    project_id: Optional[str] = None,
) -> int:
    """Remove all neg-signal rows for *domain* under *tenant_id*.

    Uses a DML DELETE query.  Returns the number of rows deleted.
    Raises on BQ failure so callers can surface errors.

    Args:
        tenant_id:  Tenant UID.
        domain:     Root domain or shared-platform path to unsuppress.
        project_id: GCP project ID (falls back to env).
    """
    import os
    from google.cloud import bigquery as _bq_lib

    _pid = project_id or os.environ.get("PROJECT_ID", "sideio-leads-v16")
    bq = _bq_lib.Client(project=_pid)
    query = """
        DELETE FROM `{project}.swarm_analytics.Negative_Signals`
        WHERE tenant_id = @tenant_id
          AND root_domain = @domain
    """.format(project=_pid)
    job_config = _bq_lib.QueryJobConfig(
        query_parameters=[
            _bq_lib.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            _bq_lib.ScalarQueryParameter("domain", "STRING", domain.lower().strip()),
        ]
    )
    result = bq.query(query, job_config=job_config).result()
    deleted = result.num_dml_affected_rows or 0
    log.info(
        "neg_signal_unsuppressed",
        domain=domain,
        tenant=tenant_id[:8],
        rows_deleted=deleted,
    )
    return deleted


def list_suppressions(
    tenant_id: str,
    project_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """List active (non-expired) neg-signal suppressions for a tenant.

    Returns a list of dicts with root_domain, entity_name, rejection_reason,
    timestamp, and expires_at for each active suppression.

    Args:
        tenant_id:  Tenant UID.
        project_id: GCP project ID (falls back to env).
        limit:      Max rows to return.
    """
    import os
    from google.cloud import bigquery as _bq_lib

    _pid = project_id or os.environ.get("PROJECT_ID", "sideio-leads-v16")
    bq = _bq_lib.Client(project=_pid)
    query = """
        SELECT root_domain, entity_name, rejection_reason, timestamp, expires_at
        FROM `{project}.swarm_analytics.Negative_Signals`
        WHERE (tenant_id = @tenant_id OR tenant_id = 'GLOBAL')
          AND (expires_at IS NULL
               OR expires_at > CURRENT_TIMESTAMP())
        GROUP BY root_domain, entity_name, rejection_reason, timestamp, expires_at
        ORDER BY timestamp DESC
        LIMIT @limit
    """.format(project=_pid)
    job_config = _bq_lib.QueryJobConfig(
        query_parameters=[
            _bq_lib.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            _bq_lib.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    rows = list(bq.query(query, job_config=job_config).result())
    results = [
        {
            "root_domain":      r["root_domain"],
            "entity_name":      r["entity_name"],
            "rejection_reason": r["rejection_reason"],
            "timestamp":        str(r["timestamp"]) if r["timestamp"] else None,
            "expires_at":       str(r["expires_at"]) if r.get("expires_at") else None,
        }
        for r in rows
    ]
    log.info(
        "neg_signal_suppressions_listed",
        tenant=tenant_id[:8],
        count=len(results),
    )
    return results
