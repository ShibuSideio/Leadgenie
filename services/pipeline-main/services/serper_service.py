"""
Pipeline-main — Serper search service.

Extracted verbatim from ``main.py`` (V22 monolith) with the following changes:

V23 changes:
  - All gRPC clients obtained via lazy accessors (``get_sm_client()``) — never
    at module scope, safe under Gunicorn pre-fork.
  - ``search_serper()`` emits structured JSON logs via ``core.logging`` instead
    of ``print()``.
  - ``_update_circuit_telemetry()`` calls are preserved (imported from telemetry).
"""
from __future__ import annotations

import json
from urllib.parse import urlparse
from typing import Optional

import httpx
from tenacity import (
    retry, wait_exponential, stop_after_attempt,
    retry_if_exception,
)

from core.logging import get_logger   # type: ignore[import]
from core.config import SERPER_API_KEY_NAME  # type: ignore[import]
from core.clients import get_sm_client, get_serper_key  # type: ignore[import]

log = get_logger("pipeline.serper")

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

SOCIAL_DOMAINS: set[str] = {
    "reddit.com", "linkedin.com", "facebook.com",
    "instagram.com", "x.com", "twitter.com",
    "quora.com", "youtube.com", "team-bhp.com",
}

_ENRICHMENT_SOCIAL_BLACKLIST = [
    "reddit.com", "facebook.com", "instagram.com", "youtube.com",
    "linkedin.com", "quora.com", "twitter.com", "x.com", "medium.com",
]

_B2C_VECTORS = [
    "Reddit B2C", "Quora B2C", "Google Maps B2C",
    "TripAdvisor B2C", "YouTube B2C", "Facebook Groups B2C",
]

_ENTERPRISE_DOMAINS = [
    "ibm.com", "amazon.com", "microsoft.com",
    "g2.com", "capterra.com", "zoominfo.com",
]

_NOISE_PATHS    = ["/legal", "/pricing", "/docs", "/author/", "/login"]
_NOISE_SNIPPETS = ["sign in", "access denied", "forgot password", "please enable cookies"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_root_domain(url: str) -> str:
    """Extract root domain from a URL, stripping www."""
    try:
        netloc = urlparse(url).netloc.lower()
        if not netloc:
            netloc = urlparse("http://" + url).netloc.lower()
        return netloc.replace("www.", "")
    except Exception:
        return ""


def filter_serper_noise(serper_results: list) -> list:
    """Remove enterprise, noise-path, and bot-page results from Serper output."""
    clean = []
    for r in serper_results:
        link    = r.get("link", "").lower()
        snippet = r.get("snippet", "").lower()
        if any(d in link for d in _ENTERPRISE_DOMAINS):
            continue
        if any(p in link for p in _NOISE_PATHS):
            continue
        if any(s in snippet for s in _NOISE_SNIPPETS):
            continue
        clean.append(r)
    return clean


def _get_serper_api_key() -> str:
    """Fetch Serper API key — uses process-lifetime cache from core.clients.

    SF-004 fix: previously called Secret Manager on every invocation.
    get_serper_key() caches the result for the lifetime of the container,
    eliminating 40+ Secret Manager RPCs per dispatch task batch.
    """
    return get_serper_key(SERPER_API_KEY_NAME)


# ---------------------------------------------------------------------------
# Primary search function
# ---------------------------------------------------------------------------

def search_serper(
    query: str,
    location: Optional[str] = None,
    gl: Optional[str] = None,
) -> list:
    """Execute a Serper Google Search query with tenacity 429-retry.

    Uses a 4-attempt exponential backoff targeting only HTTP 429 responses.
    Auth failures (401/403) and server errors (5xx) are not retried.

    On all-retries-exhausted: emits circuit telemetry and returns [].

    Args:
        query:    Full search query string (may include site: operators).
        location: Serper ``location`` field (optional, e.g. ``"India"``).
        gl:       Serper ``gl`` field / ISO country code (optional).

    Returns:
        List of organic result dicts from Serper.  Empty on any failure.
    """
    # Import telemetry locally to avoid circular imports
    from services.telemetry import update_circuit_telemetry  # type: ignore[import]

    api_key = _get_serper_api_key()
    url     = "https://google.serper.dev/search"

    payload_dict: dict = {"q": query, "num": 20}
    if location:
        payload_dict["location"] = location
    if gl:
        payload_dict["gl"] = gl

    payload = json.dumps(payload_dict)
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    def _is_rate_limited(exc: BaseException) -> bool:
        return (
            isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code == 429
        )

    @retry(
        wait=wait_exponential(multiplier=2, min=4, max=32),
        stop=stop_after_attempt(4),
        retry=retry_if_exception(_is_rate_limited),
        reraise=False,
    )
    def _do_post():
        r = httpx.post(url, headers=headers, data=payload, timeout=30)
        if r.status_code == 429:
            r.raise_for_status()
        if r.status_code == 200:
            return r.json().get("organic", [])
        log.warning("serper_non_retryable", status=r.status_code, body=r.text[:200])
        return []

    try:
        return _do_post()
    except Exception as exc:
        log.error("serper_all_retries_exhausted", query=query[:60], error=str(exc))
        update_circuit_telemetry("serper_429")
        return []


# ---------------------------------------------------------------------------
# Enrichment Gatekeeper
# ---------------------------------------------------------------------------

def deep_context_serper_dork(
    domain: str,
    tenant_id: str,
    sourcing_vector: str = "Classic B2B",
    source_url: str = "",
) -> tuple[str, bool]:
    """Fetch contextual GMB / social / hiring signals for a domain via Serper.

    V14.4 HOTFIX: Gatekeeper skips social domains and B2C vectors.
    linkedin.com/company/ URLs bypass the social blacklist — treated as B2B.

    Args:
        domain:          Root domain string.
        tenant_id:       Tenant UID for usage metering.
        sourcing_vector: Campaign sourcing vector label.
        source_url:      Full original URL (used for linkedin company detection).

    Returns:
        ``(context_data_string, hiring_intent_found)``
        ``("", False)`` if gatekeeper blocks enrichment.
    """
    from core.clients import get_db  # type: ignore[import]
    from google.cloud import firestore  # type: ignore[import]

    if not domain:
        return "", False

    _source_lower      = (source_url or "").lower()
    _is_linkedin_co    = "linkedin.com/company/" in _source_lower

    if not _is_linkedin_co:
        cleaned = domain.lower().replace("www.", "")
        for blocked in _ENRICHMENT_SOCIAL_BLACKLIST:
            if blocked in cleaned:
                log.info("enrichment_gated_social", domain=domain)
                return "", False
    else:
        log.info("enrichment_linkedin_company_bypass", source_url=source_url)

    if sourcing_vector in _B2C_VECTORS:
        log.info("enrichment_gated_b2c_vector", domain=domain, vector=sourcing_vector)
        return "", False

    api_key = _get_serper_api_key()
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    context_data: list[str] = []

    def _fetch(serper_url: str, body: dict) -> dict:
        try:
            get_db().collection("usage_metrics").document(tenant_id).set(
                {"serper_searches": firestore.Increment(1)}, merge=True
            )
            resp = httpx.post(serper_url, headers=headers, json=body, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}

    gmb_data = _fetch("https://google.serper.dev/places", {"q": domain, "num": 3})
    for place in gmb_data.get("places", []):
        context_data.append(
            f"[GMB] Rating: {place.get('rating', 'N/A')}, "
            f"Reviews: {place.get('ratingCount', 'N/A')}, "
            f"Address: {place.get('address', 'N/A')}"
        )

    social_q    = f'site:linkedin.com/company OR site:facebook.com "{domain}"'
    social_data = _fetch("https://google.serper.dev/search", {"q": social_q, "num": 3})
    for org in social_data.get("organic", []):
        context_data.append(f"[SOCIAL] {org.get('snippet', '')}")

    hiring_q    = (
        f'site:naukri.com/job-listings OR site:instahyre.com/job OR '
        f'site:linkedin.com/jobs OR site:indeed.com/cmp "{domain}"'
    )
    hiring_data = _fetch("https://google.serper.dev/search", {"q": hiring_q, "num": 3})

    hiring_sigs = [
        "we are hiring", "job description", "apply today",
        "openings", "careers", "looking for", "lakh", "lpa", "fresher",
    ]
    hiring_intent = False
    for job in hiring_data.get("organic", []):
        snippet_lc = job.get("snippet", "").lower()
        context_data.append(f"[HIRING] {snippet_lc}")
        if any(sig in snippet_lc for sig in hiring_sigs):
            hiring_intent = True

    context_str = " | ".join(context_data)[:3000]
    return context_str, hiring_intent
