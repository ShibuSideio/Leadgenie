"""
Pipeline-main — Serper search service.

Extracted verbatim from ``main.py`` (V22 monolith) with the following changes:

V23 changes:
  - All gRPC clients obtained via lazy accessors (``get_sm_client()``) — never
    at module scope, safe under Gunicorn pre-fork.
  - ``search_serper()`` emits structured JSON logs via ``core.logging`` instead
    of ``print()``.
  - ``_update_circuit_telemetry()`` calls are preserved (imported from telemetry).

V23.4 changes (Serper Audit Telemetry):
  - ``search_serper()`` now accepts optional ``campaign_id`` and ``tenant_id``
    kwargs.  After each successful call it fires a background audit row to the
    orchestrator broker (POST /api/internal/telemetry/serper-audit) via a daemon
    thread — **zero latency** added to the active scraping loop.
  - The audit payload captures: raw_query, serper_parameters, result_count,
    credit_cost, and engine (always 'search' for this function).
"""
from __future__ import annotations

import json
import os
import re
import threading
import concurrent.futures as _cf
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


def sanitize_query(query: str) -> str:
    """Sanitize the search query to remove any patterns blocked by Serper free tier.

    Specifically, filters out tokens containing forbidden social domains or their names,
    adjusting parentheses and logical operators (AND/OR/NOT) to prevent syntax errors.
    """
    if not query:
        return ""

    forbidden = ["linkedin", "facebook", "twitter", "instagram", "reddit", "quora", "youtube", "x.com"]

    # Matches quoted strings (possibly with prefix like -site: or -intitle:) or parentheses or words
    token_re = re.compile(r'([^\s()"]*"[^"]*"|[()]|[^\s()"]+)')

    tokens = []
    for match in token_re.finditer(query):
        tokens.append(match.group(0))

    clean_tokens = []
    for token in tokens:
        token_lower = token.lower()
        is_forbidden = False
        for f in forbidden:
            if f in token_lower:
                is_forbidden = True
                break
        if is_forbidden:
            continue
        clean_tokens.append(token)

    # Clean up consecutive logical operators
    final_tokens = []
    for token in clean_tokens:
        if token in ("AND", "OR", "NOT") and final_tokens and final_tokens[-1] in ("AND", "OR", "NOT"):
            continue
        final_tokens.append(token)

    # Remove leading/trailing AND/OR/NOT
    while final_tokens and final_tokens[0] in ("AND", "OR"):
        final_tokens.pop(0)
    while final_tokens and final_tokens[-1] in ("AND", "OR", "NOT"):
        final_tokens.pop()

    # Reassemble tokens
    result = ""
    for token in final_tokens:
        if token in ("(", ")"):
            result += token
        else:
            if result and not result.endswith("("):
                result += " " + token
            else:
                result += token

    # Clean up empty parentheses and trailing operators inside parentheses
    while True:
        new_result = re.sub(r'\(\s*\)', '', result)
        new_result = re.sub(r'\(\s*(AND|OR|NOT)\s*\)', '', new_result)
        new_result = re.sub(r'\(\s*(AND|OR|NOT)\s+', '(', new_result)
        new_result = re.sub(r'\s+(AND|OR|NOT)\s*\)', ')', new_result)
        new_result = re.sub(r'\s+', ' ', new_result).strip()
        if new_result == result:
            break
        result = new_result

    return result


def _get_serper_api_key() -> str:
    """Fetch Serper API key — uses process-lifetime cache from core.clients.

    SF-004 fix: previously called Secret Manager on every invocation.
    get_serper_key() caches the result for the lifetime of the container,
    eliminating 40+ Secret Manager RPCs per dispatch task batch.
    """
    return get_serper_key(SERPER_API_KEY_NAME)


# ---------------------------------------------------------------------------
# Serper Audit Telemetry — async fire-and-forget (V23.4.1 — Zero-Trust OIDC)
# ---------------------------------------------------------------------------
_ORCHESTRATOR_URL: str = os.environ.get("ORCHESTRATOR_URL", "").rstrip("/")

# ── OIDC token cache (thread-safe, 55-min TTL) ─────────────────────────────
# GCE metadata server tokens expire after 60 minutes.  55-min TTL absorbs
# clock-skew and gives the refresh a 5-minute safety margin.
import threading as _th_oidc
_OIDC_CACHE_LOCK    = _th_oidc.Lock()
_oidc_token_value:  str   = ""
_oidc_token_expiry: float = 0.0  # monotonic timestamp


def _get_oidc_token(audience: str) -> str:
    """Return a valid Google OIDC identity token for *audience*.

    Fetches from the GCE instance metadata server — always available on
    Cloud Run, GKE, and GCE.  Result is cached for 55 minutes.

    Falls back to google-auth library (ADC / key file) if the metadata
    server is unavailable (e.g. local dev with GOOGLE_APPLICATION_CREDENTIALS).
    Returns "" on all failures so callers degrade gracefully.

    Args:
        audience: Base URL of the target service, e.g.
                  "https://orchestrator-xyz-uc.a.run.app".  Must exactly
                  match the audience the orchestrator validates against.
    """
    import time as _time

    global _oidc_token_value, _oidc_token_expiry

    # Fast path — no lock needed if cache is warm
    _now = _time.monotonic()
    if _oidc_token_value and _now < _oidc_token_expiry:
        return _oidc_token_value

    with _OIDC_CACHE_LOCK:
        # Double-checked locking: re-test after acquiring
        _now = _time.monotonic()
        if _oidc_token_value and _now < _oidc_token_expiry:
            return _oidc_token_value

        try:
            # Primary: GCE metadata server (always present on Cloud Run)
            import urllib.request as _url_req
            _meta_url = (
                "http://metadata.google.internal/computeMetadata/v1/instance"
                f"/service-accounts/default/identity"
                f"?audience={audience}&format=full"
            )
            _req = _url_req.Request(_meta_url, headers={"Metadata-Flavor": "Google"})
            with _url_req.urlopen(_req, timeout=3) as _resp:
                _token = _resp.read().decode("utf-8").strip()
            _oidc_token_value  = _token
            _oidc_token_expiry = _time.monotonic() + (55 * 60)
            log.debug("oidc_token_refreshed_metadata", audience=audience[:50])
            return _oidc_token_value

        except Exception as _meta_err:
            # Fallback: google-auth ADC (key file / workload identity)
            try:
                import google.auth as _gauth
                from google.auth.transport import requests as _gauth_req
                _creds, _ = _gauth.default(scopes=["openid"])
                _creds.refresh(_gauth_req.Request())
                _token = getattr(_creds, "id_token", None) or getattr(_creds, "token", "")
                if _token:
                    _oidc_token_value  = _token
                    _oidc_token_expiry = _time.monotonic() + (55 * 60)
                    log.debug("oidc_token_refreshed_adc", audience=audience[:50])
                    return _oidc_token_value
            except Exception as _adc_err:
                log.warning(
                    "oidc_token_fetch_failed",
                    metadata_err=str(_meta_err)[:120],
                    adc_err=str(_adc_err)[:120],
                    action="Telemetry will be dropped — check lead-pipeline-sa IAM."
                )
            return ""


def _push_serper_audit(
    campaign_id: str,
    tenant_id: str,
    raw_query: str,
    serper_parameters: dict,
    result_count: int,
    status_code: int = 200,
    error_message: str = "",
    engine: str = "search",
) -> None:
    """Push one audit row to the orchestrator BQ broker via Zero-Trust OIDC.

    Runs in a daemon thread — never blocks the calling scrape loop.
    Silently swallowed on any exception (non-critical telemetry path).

    V23.4 bug (fixed in V23.4.1):
        Previously called httpx.post() with no Authorization header.
        The orchestrator's _verify_oidc() silently returned HTTP 200 with
        {"ok": false, "reason": "auth_failed"}, so zero rows reached BigQuery.

    V23.4.1 fix:
        Fetches a Google OIDC identity token from the GCE metadata server
        (audience = ORCHESTRATOR_URL), caches it for 55 minutes, and attaches
        it as "Authorization: Bearer <token>".  _verify_oidc() validates the
        token signature via Google public certs and admits the request.
    """
    if not _ORCHESTRATOR_URL:
        return  # env var not set — skip silently (local/dev environments)
    try:
        import datetime as _dt
        payload = {
            "table":              "serper_audit_logs",  # broker routing key
            "campaign_id":        campaign_id or "",
            "tenant_id":          tenant_id   or "",
            "raw_query":          raw_query,
            "serper_parameters":  serper_parameters,
            "result_count":       result_count,
            "credit_cost":        1,
            "engine":             engine,
            "serper_status_code": status_code,
            "error_message":      error_message or None,
            "timestamp":          _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        # ── Zero-Trust OIDC — attach identity token ──────────────────────────
        _headers: dict = {"Content-Type": "application/json"}
        _token = _get_oidc_token(_ORCHESTRATOR_URL)
        if _token:
            _headers["Authorization"] = f"Bearer {_token}"
        else:
            log.warning(
                "serper_audit_no_oidc_token",
                orchestrator=_ORCHESTRATOR_URL[:50],
                action="Row will be rejected by orchestrator auth — check SA.",
            )

        # ── Non-fatal HTTP POST — telemetry must never crash the scraping loop ────
        # We capture the response and log at WARNING on failure, but we do NOT
        # call raise_for_status(). A 500 from the orchestrator means BQ rejected
        # the row; we log it so the issue is visible in Cloud Logging, then exit
        # cleanly. The scraping loop continues regardless.
        _resp = httpx.post(
            f"{_ORCHESTRATOR_URL}/api/internal/telemetry/serper-audit",
            json=payload,
            headers=_headers,
            timeout=5,
        )
        if _resp.status_code != 200:
            log.warning(
                "serper_audit_broker_non_200",
                status=_resp.status_code,
                body=_resp.text[:200],
                action="Telemetry row dropped. BQ schema mismatch or orchestrator error."
            )
    except Exception as _te:
        # Network-level failure (timeout, DNS, connection refused).
        # Log at WARNING so failures appear in production Cloud Logging.
        # Never raise — telemetry is non-critical fire-and-forget.
        log.warning("serper_audit_push_failed", error=str(_te)[:200])


def _async_serper_audit(**kwargs) -> None:
    """Fire-and-forget wrapper: launches _push_serper_audit in a daemon thread."""
    try:
        t = threading.Thread(target=_push_serper_audit, kwargs=kwargs, daemon=True)
        t.start()
    except Exception:
        pass  # never crash the scraping loop


# ---------------------------------------------------------------------------
# Primary search function
# ---------------------------------------------------------------------------

def search_serper(
    query: str,
    location: Optional[str] = None,
    gl: Optional[str] = None,
    *,
    campaign_id: str = "",
    tenant_id: str = "",
) -> list:
    """Execute a Serper Google Search query with tenacity 429-retry.

    Uses a 4-attempt exponential backoff targeting only HTTP 429 responses.
    Auth failures (401/403) and server errors (5xx) are not retried.

    On all-retries-exhausted: emits circuit telemetry, fires a failed audit
    row, and returns [].

    Args:
        query:       Full search query string (may include site: operators).
        location:    Serper ``location`` field (optional, e.g. ``"India"``).
        gl:          Serper ``gl`` field / ISO country code (optional).
        campaign_id: Campaign context for BQ audit telemetry (optional).
        tenant_id:   Tenant context for BQ audit telemetry (optional).

    Returns:
        List of organic result dicts from Serper.  Empty on any failure.
    """
    # Import telemetry locally to avoid circular imports
    from services.telemetry import update_circuit_telemetry  # type: ignore[import]

    # Pre-emptively sanitize query for Serper free-tier compliance
    sanitized = sanitize_query(query)
    if not sanitized:
        log.warning("serper_query_sanitized_to_empty", original_query=query)
        return []

    if sanitized != query:
        log.info("serper_query_sanitized", original=query, sanitized=sanitized)
        query = sanitized

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
        results = _do_post()
        # ── V23.4: Async audit telemetry — zero-latency fire-and-forget ──────
        _async_serper_audit(
            campaign_id       = campaign_id,
            tenant_id         = tenant_id,
            raw_query         = query,
            serper_parameters = payload_dict,
            result_count      = len(results),
            status_code       = 200,
            engine            = "search",
        )
        return results
    except Exception as exc:
        log.error("serper_all_retries_exhausted", query=query[:60], error=str(exc))
        update_circuit_telemetry("serper_429")
        # ── Audit row for failed call ─────────────────────────────────────────
        _async_serper_audit(
            campaign_id       = campaign_id,
            tenant_id         = tenant_id,
            raw_query         = query,
            serper_parameters = payload_dict,
            result_count      = 0,
            status_code       = 429,
            error_message     = str(exc)[:500],
            engine            = "search",
        )
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

    # Ensure we are working with the root domain.
    # If a full URL is passed to domain (like in dispatch.py line 773), extract the root domain.
    if "://" in domain or "/" in domain or "." not in domain:
        domain = extract_root_domain(domain)
        if not domain:
            return "", False

    # Check against the social domains blacklist.
    # Social domains and B2C vectors bypass enrichment since they don't contain B2B lead info.
    cleaned = domain.lower().replace("www.", "")
    for blocked in _ENRICHMENT_SOCIAL_BLACKLIST:
        if blocked in cleaned:
            log.info("enrichment_gated_social", domain=domain)
            return "", False

    if sourcing_vector in _B2C_VECTORS:
        log.info("enrichment_gated_b2c_vector", domain=domain, vector=sourcing_vector)
        return "", False

    api_key = _get_serper_api_key()
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    context_data: list[str] = []

    # BUG-S1 FIX: Previously 3 sequential Serper calls with timeout=15 each = 45s max.
    # Fix: Run all 3 calls in parallel via ThreadPoolExecutor. Max wall time ~15s.
    # BUG-S2 FIX: Usage metrics Firestore write was inside _fetch() per call = 3 RPCs.
    # Fix: Single batched write at the end (1 RPC total).
    def _fetch_parallel(serper_url: str, body: dict) -> dict:
        """Fire a Serper request. No Firestore write here (batched externally)."""
        try:
            resp = httpx.post(serper_url, headers=headers, json=body, timeout=12)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}

    tasks = [
        ("https://google.serper.dev/places",  {"q": domain, "num": 3}),
        ("https://google.serper.dev/search",  {"q": f'company profile OR social media "{domain}"', "num": 3}),
        ("https://google.serper.dev/search",  {
            "q": f'job openings OR careers "{domain}"',
            "num": 3,
        }),
    ]

    with _cf.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_fetch_parallel, url, body) for url, body in tasks]
        results = []
        for fut in futures:
            try:
                results.append(fut.result(timeout=13))
            except Exception:
                results.append({})

    gmb_data, social_data, hiring_data = results[0], results[1], results[2]

    for place in gmb_data.get("places", []):
        context_data.append(
            f"[GMB] Rating: {place.get('rating', 'N/A')}, "
            f"Reviews: {place.get('ratingCount', 'N/A')}, "
            f"Address: {place.get('address', 'N/A')}"
        )

    for org in social_data.get("organic", []):
        context_data.append(f"[SOCIAL] {org.get('snippet', '')}")

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

    # BUG-S2 FIX: Single batched Firestore write for all 3 Serper calls.
    try:
        from google.cloud import firestore as _fs  # type: ignore[import]
        get_db().collection("usage_metrics").document(tenant_id).set(
            {"serper_searches": _fs.Increment(len(tasks))}, merge=True
        )
    except Exception:
        pass

    context_str = " | ".join(context_data)[:3000]
    return context_str, hiring_intent
