"""
Pipeline-main — Negative Signal Shield service.

Fetches the top suppressed domains and entity names from
``swarm_analytics.Negative_Signals`` to inject as ``-site:`` and
``-intitle:`` operators into every Serper query batch.

Design contract (V22 TSD §25.2.1, amended 2026-06-20):
  * Hard 5-second wall-clock timeout via ThreadPoolExecutor (R2 FIX: raised
    from 3s to tolerate minor GCP latency spikes without silent degradation).
  * In-memory LRU cache with 10-minute TTL — avoids repeated BQ queries
    within the same container lifetime. On BQ timeout/failure, serves the
    last-known-good cached result instead of returning empty lists.
  * Returns ``([], [])`` ONLY on first-ever call failure for an unseen tenant.
  * The scraping hot loop MUST NEVER block waiting for BigQuery.
"""
from __future__ import annotations

import concurrent.futures
import time
from typing import Optional

from core.logging import get_logger
from core.config import PROJECT_ID, NEG_SHIELD_BQ_TIMEOUT_S

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# R2 FIX (2026-06-20): In-memory LRU cache with 10-minute TTL
# ---------------------------------------------------------------------------
# Prevents silent degradation when BQ latency spikes cause repeated timeouts.
# On failure, the most recently fetched result for a tenant is served instead
# of returning ([], []) — which would silently disable the negative shield.

_CACHE_TTL_S: float = 600.0  # 10 minutes

_cache: dict[str, tuple[float, list[str], list[str]]] = {}


def _cache_get(tenant_id: str) -> Optional[tuple[list[str], list[str]]]:
    """Return cached (domains, entities) if TTL has not expired, else None."""
    entry = _cache.get(tenant_id)
    if entry is None:
        return None
    ts, domains, entities = entry
    if time.monotonic() - ts > _CACHE_TTL_S:
        return None  # expired — force BQ re-fetch
    return domains, entities


def _cache_get_stale(tenant_id: str) -> Optional[tuple[list[str], list[str]]]:
    """Return cached result even if expired (last-known-good fallback)."""
    entry = _cache.get(tenant_id)
    if entry is None:
        return None
    _, domains, entities = entry
    return domains, entities


def _cache_put(tenant_id: str, domains: list[str], entities: list[str]) -> None:
    """Store result in cache with current timestamp."""
    _cache[tenant_id] = (time.monotonic(), domains, entities)
    # Evict oldest entries if cache grows beyond 200 tenants
    if len(_cache) > 200:
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest_key, None)


# ---------------------------------------------------------------------------
# R2 FIX: Raised timeout from 3s → 5s
# ---------------------------------------------------------------------------
# The original 3s ceiling was too aggressive for GCP regions with occasional
# 2-4s cold-start latency on BQ slot acquisition. At 3s, minor spikes
# silently disabled the neg shield for the entire produce cycle.
# 5s absorbs the p95 latency while still meeting the <10s produce budget.
_EFFECTIVE_TIMEOUT_S: float = max(NEG_SHIELD_BQ_TIMEOUT_S, 5.0)


def fetch_neg_shield(tenant_id: str) -> tuple[list[str], list[str]]:
    """Fetch top 20 suppressed domains and entity names from BigQuery.

    Uses an in-memory cache with 10-minute TTL. On BQ timeout or failure,
    returns the last-known-good cached result instead of empty lists.

    Args:
        tenant_id: Tenant UID — scopes to this tenant + GLOBAL signals.

    Returns:
        Tuple of ``(blocked_domains, blocked_entities)``, each a deduplicated
        list of up to 20 strings.  Returns ``([], [])`` only if no cached
        result exists and BQ also fails.
    """
    # Fast path: serve from cache if TTL has not expired
    cached = _cache_get(tenant_id)
    if cached is not None:
        log.debug("neg_shield_cache_hit", tenant=tenant_id[:8])
        return cached

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
        rows = list(job.result(timeout=_EFFECTIVE_TIMEOUT_S))
        blocked_domains = list({r["root_domain"] for r in rows if r["root_domain"]})
        blocked_entities = list({r["entity_name"] for r in rows if r["entity_name"]})
        return blocked_domains, blocked_entities

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_run)
            blocked_domains, blocked_entities = fut.result(timeout=_EFFECTIVE_TIMEOUT_S + 0.5)
        _cache_put(tenant_id, blocked_domains, blocked_entities)
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
            timeout_s=_EFFECTIVE_TIMEOUT_S,
            tenant=tenant_id[:8],
            action="Serving last-known-good cache if available.",
        )
        stale = _cache_get_stale(tenant_id)
        if stale is not None:
            log.info("neg_shield_stale_cache_served", tenant=tenant_id[:8],
                     domains=len(stale[0]), entities=len(stale[1]))
            return stale
        return [], []
    except Exception as exc:
        log.warning("neg_shield_fetch_failed", error=str(exc), tenant=tenant_id[:8],
                    action="Serving last-known-good cache if available.")
        stale = _cache_get_stale(tenant_id)
        if stale is not None:
            log.info("neg_shield_stale_cache_served", tenant=tenant_id[:8],
                     domains=len(stale[0]), entities=len(stale[1]))
            return stale
        return [], []
