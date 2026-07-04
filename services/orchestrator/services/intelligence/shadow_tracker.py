"""
Orchestrator — Shadow Tracker intelligence service.

Extracted from the monolithic main.py N-gram accumulation block.

.. deprecated:: V24.6
   This module is DEPRECATED. Use ``core.helpers._async_shadow_track`` and
   ``core.helpers._do_shadow_track`` instead. All public functions in this
   module now delegate to their ``core.helpers`` counterparts and will be
   removed in a future release.

Responsibilities:
  1. ``_extract_ngrams()``       — Pure Python NLP, zero external dependencies.
  2. ``_do_shadow_track()``      — Synchronous BigQuery MERGE upsert.
  3. ``_async_shadow_track()``   — Fire-and-forget daemon thread wrapper.

Design contract (V22 TSD §25.1.2, Design Invariant #16):
  * Shadow Tracker threads MUST be ``daemon=True``.
  * The HTTP 200 on lead approval MUST never wait for BigQuery.
  * BQ failures MUST NOT propagate to the response path.
"""
from __future__ import annotations

import warnings

warnings.warn(
    "services.intelligence.shadow_tracker is deprecated. "
    "Use core.helpers._async_shadow_track instead.",
    DeprecationWarning,
    stacklevel=2,
)

import re
import threading
import datetime
from collections import Counter
from typing import Optional

from core.logging import get_logger
from core.helpers import _async_shadow_track as _helpers_async_shadow_track  # type: ignore[import]

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Stop-word list — filters common function words before N-gram windowing
# ---------------------------------------------------------------------------
_STOP_WORDS: frozenset[str] = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "her",
    "was", "one", "our", "out", "day", "get", "has", "him", "his", "how",
    "its", "may", "new", "now", "old", "see", "she", "too", "use", "way",
    "who", "boy", "did", "man", "put", "say", "that", "this", "with", "have",
    "from", "they", "know", "want", "been", "good", "much", "some", "time",
    "very", "when", "come", "here", "just", "like", "long", "make", "many",
    "more", "only", "over", "such", "take", "than", "them", "then", "well",
    "were", "will", "also", "into", "most", "their", "there", "these",
    "what", "your", "about", "which", "would", "could", "after", "being",
    "other", "those", "where", "while",
})


# ---------------------------------------------------------------------------
# V24.2 (L8-2): PII scrubbing — remove personal identifiers before BQ write
# GDPR Article 5 (data minimisation) + Article 25 (privacy by design)
# ---------------------------------------------------------------------------
import re as _re
_EMAIL_RE = _re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
_PHONE_RE = _re.compile(r'\b(?:\+?\d[\d\s\-\.]{7,14}\d)\b')
_NAME_RE  = _re.compile(
    r'\b(?:Mr|Mrs|Ms|Dr|Prof)\s+[A-Z][a-z]+(\s+[A-Z][a-z]+)?\b'
)

def _scrub_pii(text: str) -> str:
    """Remove personal identifiers from lead text before BigQuery ingestion.

    Scrubs:
    - Email addresses \u2192 [EMAIL]
    - Phone numbers   \u2192 [PHONE]
    - Salutation-prefixed names (Mr. John Smith) \u2192 [NAME]

    Args:
        text: Raw lead text (pain_point + dm).

    Returns:
        Text with PII patterns replaced by placeholder tokens.
    """
    if not text:
        return text
    text = _EMAIL_RE.sub('[EMAIL]', text)
    text = _PHONE_RE.sub('[PHONE]', text)
    text = _NAME_RE.sub('[NAME]', text)
    return text


def extract_ngrams(
    text: str,
    n_min: int = 2,
    n_max: int = 3,
    top_k: int = 5,
) -> list[str]:
    """Extract top-k most frequent N-grams from *text*.

    .. deprecated:: V24.6
       Use ``core.helpers`` equivalent. This wrapper delegates and will be removed.

    Filters stop-words before creating N-gram windows to surface
    meaningful buyer-syntax phrases (e.g. ``"struggling with churn"``).

    Args:
        text:  Raw lead text (``pain_point`` + ``dm`` fields).
        n_min: Minimum N-gram window size (default: 2).
        n_max: Maximum N-gram window size (default: 3).
        top_k: Maximum number of N-grams to return (default: 5).

    Returns:
        List of lowercase N-gram strings ordered by frequency.
    """
    log.warning("DEPRECATED: Use core.helpers._async_shadow_track instead")
    if not text:
        return []
    tokens = re.findall(r"\b[a-z]{3,}\b", text.lower())
    clean = [t for t in tokens if t not in _STOP_WORDS]
    ngrams: list[str] = []
    for n in range(n_min, n_max + 1):
        for i in range(len(clean) - n + 1):
            ngrams.append(" ".join(clean[i : i + n]))
    counter = Counter(ngrams)
    return [ng for ng, _ in counter.most_common(top_k)]


def _do_shadow_track(
    persona_category: str,
    pain_point: str,
    tenant_id: str,
    project_id: str,
    event_type: str = "occurrence",  # "occurrence" | "conversion" | "rejection"
) -> None:
    """Synchronous BQ MERGE upsert for Intent_Keywords.

    .. deprecated:: V24.6
       Delegates to ``core.helpers._do_shadow_track``. Will be removed.

    Runs exclusively inside a daemon thread.  Never raises — all failures
    are logged at WARNING and swallowed (Design Invariant #16).

    Args:
        persona_category: Persona name / ICP bucket.
        pain_point:       Lead pain_point + dm text to extract N-grams from.
        tenant_id:        Tenant UID.
        project_id:       GCP project ID for BigQuery.
    """
    log.warning("DEPRECATED: Use core.helpers._async_shadow_track instead")
    try:
        from google.cloud import bigquery as _bq_lib
        # V24.2 (L8-2): Scrub PII before N-gram extraction and BQ write.
        scrubbed_text = _scrub_pii(pain_point)
        ngrams = extract_ngrams(scrubbed_text)
        if not ngrams:
            log.info(
                "shadow_tracker_no_ngrams",
                persona=persona_category,
                tenant=tenant_id[:8],
            )
            return

        bq = _bq_lib.Client(project=project_id)
        table_ref = f"`{project_id}.swarm_analytics.Intent_Keywords`"
        now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        merge_query = f"""
            MERGE {table_ref} AS T
            USING (
                SELECT * FROM UNNEST([
                    {', '.join(
                        f"STRUCT(@tenant_id AS tenant_id, @cat AS persona_category, "
                        f"@ng_{i} AS n_gram, 1 AS occurrence_count, 1.0 AS yield_weight, "
                        f"TIMESTAMP('{now_iso}') AS last_seen)"
                        for i, _ in enumerate(ngrams)
                    )}
                ])
            ) AS S
            ON T.persona_category = S.persona_category
               AND T.n_gram = S.n_gram
               AND T.tenant_id = S.tenant_id
            WHEN MATCHED THEN
                UPDATE SET
                    occurrence_count = T.occurrence_count + 1,
                    -- V24.5 (L8-3): yield_weight differentiates by event quality:
                    -- conversion = +2.0 (strong signal), occurrence = +1.0 (neutral),
                    -- rejection = -0.5 (negative signal)
                    yield_weight     = T.yield_weight + @yield_delta,
                    last_seen        = S.last_seen
            WHEN NOT MATCHED THEN
                INSERT (tenant_id, persona_category, n_gram, occurrence_count, yield_weight, last_seen)
                VALUES (S.tenant_id, S.persona_category, S.n_gram, S.occurrence_count, S.yield_weight, S.last_seen)
        """
        yield_delta_map = {"conversion": 2.0, "rejection": -0.5, "occurrence": 1.0}
        yield_delta = yield_delta_map.get(event_type, 1.0)
        params = [
            _bq_lib.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
            _bq_lib.ScalarQueryParameter("cat", "STRING", persona_category),
            _bq_lib.ScalarQueryParameter("yield_delta", "FLOAT64", yield_delta),
        ] + [
            _bq_lib.ScalarQueryParameter(f"ng_{i}", "STRING", ng)
            for i, ng in enumerate(ngrams)
        ]
        job = bq.query(merge_query, job_config=_bq_lib.QueryJobConfig(query_parameters=params))
        job.result(timeout=30)
        log.info(
            "shadow_tracker_upserted",
            ngrams_count=len(ngrams),
            persona=persona_category,
            tenant=tenant_id[:8],
        )
    except Exception as exc:
        log.warning(
            "shadow_tracker_failed",
            error=str(exc),
            tenant=tenant_id[:8],
            persona=persona_category,
        )


def async_shadow_track(
    persona_category: str,
    pain_point: str,
    tenant_id: str,
    project_id: str,
    event_type: str = "occurrence",
) -> None:
    """Spawn a daemon thread to upsert N-grams to Intent_Keywords.

    .. deprecated:: V24.6
       Delegates to ``core.helpers._async_shadow_track``. Will be removed.

    Fire-and-forget — never raises.  The HTTP 200 to the UI is never delayed.

    Args:
        persona_category: Persona name / ICP bucket.
        pain_point:       Lead text.
        tenant_id:        Tenant UID.
        project_id:       GCP project ID.
    """
    log.warning("DEPRECATED: Use core.helpers._async_shadow_track instead")
    try:
        # Delegate to the canonical implementation in core.helpers
        _helpers_async_shadow_track(persona_category, pain_point, tenant_id)
    except Exception as exc:
        log.warning("shadow_tracker_thread_spawn_failed", error=str(exc))
