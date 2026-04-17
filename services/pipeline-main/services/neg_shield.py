"""
Pipeline-main — Negative Signal Shield service.

Fetches the top suppressed domains and entity names from
``swarm_analytics.Negative_Signals`` to inject as ``-site:`` and
``-intitle:`` operators into every Serper query batch.

Design contract (V22 TSD §25.2.1):
  * Hard 3-second wall-clock timeout via ThreadPoolExecutor.
  * Returns ``([], [])`` on ANY failure — pipeline degrades gracefully.
  * The scraping hot loop MUST NEVER block waiting for BigQuery.
"""
from __future__ import annotations

import concurrent.futures

from core.logging import get_logger
from core.config import PROJECT_ID, NEG_SHIELD_BQ_TIMEOUT_S

log = get_logger(__name__)


def fetch_neg_shield(tenant_id: str) -> tuple[list[str], list[str]]:
    """Fetch top 20 suppressed domains and entity names from BigQuery.

    Executes a parameterised query against ``swarm_analytics.Negative_Signals``
    with a hard 3-second timeout to guarantee the scraping loop is never delayed
    by BigQuery latency.

    Args:
        tenant_id: Tenant UID — scopes to this tenant + GLOBAL signals.

    Returns:
        Tuple of ``(blocked_domains, blocked_entities)``, each a deduplicated
        list of up to 20 strings.  Returns ``([], [])`` on any failure.
    """
    from google.cloud import bigquery as _bq_lib  # local to avoid cold-start overhead

    def _run() -> tuple[list[str], list[str]]:
        bq = _bq_lib.Client(project=PROJECT_ID)
        query = """
            SELECT root_domain, entity_name
            FROM `{project}.swarm_analytics.Negative_Signals`
            WHERE (tenant_id = @tenant_id OR tenant_id = 'GLOBAL')
              AND root_domain IS NOT NULL
            GROUP BY root_domain, entity_name
            ORDER BY COUNT(*) DESC
            LIMIT 20
        """.format(project=PROJECT_ID)
        job_config = _bq_lib.QueryJobConfig(
            query_parameters=[
                _bq_lib.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            ]
        )
        job = bq.query(query, job_config=job_config)
        rows = list(job.result(timeout=NEG_SHIELD_BQ_TIMEOUT_S))
        blocked_domains = list({r["root_domain"] for r in rows if r["root_domain"]})
        blocked_entities = list({r["entity_name"] for r in rows if r["entity_name"]})
        return blocked_domains, blocked_entities

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_run)
            blocked_domains, blocked_entities = fut.result(timeout=NEG_SHIELD_BQ_TIMEOUT_S + 0.5)
        log.info(
            "neg_shield_loaded",
            blocked_domains=len(blocked_domains),
            blocked_entities=len(blocked_entities),
            tenant=tenant_id[:8],
        )
        return blocked_domains, blocked_entities
    except concurrent.futures.TimeoutError:
        log.warning(
            "neg_shield_timeout",
            timeout_s=NEG_SHIELD_BQ_TIMEOUT_S,
            tenant=tenant_id[:8],
        )
        return [], []
    except Exception as exc:
        log.warning("neg_shield_fetch_failed", error=str(exc), tenant=tenant_id[:8])
        return [], []
