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
    # Social platforms
    "reddit.com", "facebook.com", "instagram.com", "youtube.com",
    "linkedin.com", "quora.com", "twitter.com", "x.com", "medium.com",
    "tiktok.com", "pinterest.com", "tumblr.com", "snapchat.com",
    # Community forums — not B2B leads
    "team-bhp.com", "skyscrapercity.com", "airliners.net",
    "stackexchange.com", "stackoverflow.com", "serverfault.com",
    "hackernews.com", "news.ycombinator.com", "slashdot.org",
    "discourse.org", "proboards.com", "phpbb.com", "vbulletin.com",
    # Content/wiki/education — not B2B leads
    "wikipedia.org", "wikia.com", "fandom.com", "archive.org",
    "academia.edu", "researchgate.net", "slideshare.net",
    # V24.1.1: Hobby/consumer domains observed in Serper logs
    "collegeconfidential.com", "nameberry.com", "garden.org",
    "contra.com",       # freelancer marketplace, not a B2B lead
    "gasoccerforum.com", "realcavsfans.com",
    "dukebasketballreport.com", "thecardboard.org",
    "volleytalk.com",   # catches volleytalk.proboards.com too
    # Classifieds / directories / reviews — not B2B leads
    "yelp.com", "yellowpages.com", "bbb.org", "trustpilot.com",
    "glassdoor.com", "indeed.com", "monster.com",
]

# V24.1.1: Forum subdomain prefixes — if domain starts with any of these,
# it's almost certainly a community forum, not a business website.
_FORUM_PREFIXES = (
    "forum.", "forums.", "talk.", "community.", "discuss.",
    "board.", "boards.", "bbs.", "chat.",
)

# V24.1.1: Keywords in domain name that strongly indicate non-business.
# Matched as substring: "gasoccerforum.com" contains "forum".
_FORUM_DOMAIN_KEYWORDS = (
    "forum", "fans", "proboards", "phpbb", "vbulletin",
    "boards", "fansite", "fandom",
)

# V24.0: Domain suffixes that indicate non-business entities.
# V24.1.1: .org now skips ALL enrichment (not just Places).
_NON_BUSINESS_SUFFIXES = (
    ".edu", ".ac.in", ".ernet.in", ".gov", ".gov.in", ".mil",
    ".org",  # most .org are non-profits/foundations, not B2B leads
)

# FIX (2026-06-21): Replaced dead platform-specific B2C list with archetype-based
# detection. The old list contained labels ("Reddit B2C", etc.) that could never
# V24.3 (L2-2): Imported from shared core.constants module.
# Previously defined inline with a "MUST stay in sync with query_brain" warning.
from core.constants import CONSUMER_ARCHETYPES as _CONSUMER_ARCHETYPES  # type: ignore[import]

def _is_consumer_archetype(vector: str) -> bool:
    """Return True if *vector* is a consumer-facing business archetype."""
    return (vector or "").upper().strip() in _CONSUMER_ARCHETYPES


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
    """Remove enterprise, noise-path, and bot-page results from Serper output.

    V24.1.1 FIX: Uses proper domain extraction and path-segment matching
    instead of substring. Prevents false positives like 'caribm.com' matching
    'ibm.com' or '/doctrine' matching '/docs'.
    """
    clean = []
    for r in serper_results:
        link    = r.get("link", "").lower()
        snippet = r.get("snippet", "").lower()
        # V24.1.1: Use root domain extraction instead of substring match
        link_domain = extract_root_domain(link)
        if link_domain in _ENTERPRISE_DOMAINS:
            continue
        # V24.1.1: Path-segment matching — check that the path segment starts with noise prefix
        try:
            link_path = urlparse(link).path.lower()
        except Exception:
            link_path = ""
        if any(link_path.startswith(p) or f"{p}/" in link_path for p in _NOISE_PATHS):
            continue
        if any(s in snippet for s in _NOISE_SNIPPETS):
            continue
        clean.append(r)
    return clean

# V24.1.1: Check env var for Serper paid tier (skips social domain stripping)
_SERPER_PAID_TIER = os.getenv("SERPER_PAID_TIER", "").lower() in ("true", "1", "yes")


def sanitize_query(query: str) -> str:
    """Sanitize the search query to remove patterns blocked by Serper free tier.

    V24.1.1 FIX: When SERPER_PAID_TIER=true, social domain stripping is skipped
    entirely. Paid tier supports site:linkedin.com and similar queries. On free
    tier, stripping is still active but now logs when positive site: operators
    are removed (to diagnose query degradation).
    """
    if not query:
        return ""

    # Sanitize wildcard domain operators (site:*.org -> site:.org)
    query = re.sub(r'(?<!\w)site:\*\.', 'site:.', query)

    # Insert missing space between quotes and opening parenthesis: "abc"(xyz) -> "abc" (xyz)
    query = re.sub(r'(?<=\")\(', ' (', query)

    # Insert missing space between alphanumeric/dots/hyphens and opening parenthesis: net(xyz) -> net (xyz)
    query = re.sub(r'([a-zA-Z0-9\.\-_])\(', r'\1 (', query)
    
    # Insert missing space between closing and opening parenthesis: )( -> ) (
    query = re.sub(r'\)(?=\()', ') ', query)

    # V24.1.1: Skip sanitization entirely on paid tier
    if _SERPER_PAID_TIER:
        return query


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
            # V24.1.1: Log when positive site: operators are stripped
            if token_lower.startswith("site:"):
                log.warning("sanitize_query_positive_site_stripped",
                            token=token[:80], query=query[:100],
                            note="Set SERPER_PAID_TIER=true to preserve social site: operators.")
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

    # Reassemble tokens - preserving proper spacing before opening parentheses
    result = ""
    for token in final_tokens:
        if token == "(":
            if result and not result.endswith("(") and not result.endswith(" "):
                result += " ("
            else:
                result += "("
        elif token == ")":
            result += ")"
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
            "timestamp":          _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    sourcing_vector: str = "",
) -> list:
    """Execute a Serper Google Search query with tenacity 429-retry.

    Uses a 4-attempt exponential backoff targeting only HTTP 429 responses.
    Auth failures (401/403) and server errors (5xx) are not retried.

    On all-retries-exhausted: emits circuit telemetry, fires a failed audit
    row, and returns [].

    Args:
        query:           Full search query string (may include site: operators).
        location:        Serper ``location`` field (optional, e.g. ``"India"``).
        gl:              Serper ``gl`` field / ISO country code (optional).
        campaign_id:     Campaign context for BQ audit telemetry (optional).
        tenant_id:       Tenant context for BQ audit telemetry (optional).
        sourcing_vector: Campaign sourcing vector label (optional).

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

    # V24.3 (L3-1): Use qdr:m (past month) instead of qdr:y (past year) for
    # consumer campaigns. Dialog-cue dorks like "pm me" or "still available"
    # target ACTIVE purchase discussions. A 1-year window retrieves stale
    # conversations where the deal is long closed. The month window prevents
    # Serper from returning abandoned Reddit threads and closed listings.
    if sourcing_vector and _is_consumer_archetype(sourcing_vector):
        payload_dict["tbs"] = "qdr:m"

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
        reraise=True,  # V24.1.1 FIX: reraise=False returned None on exhaustion → TypeError on len()
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
        if results is None:
            results = []  # V24.1.1: guard against tenacity edge cases
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
        # V24.1.1 FIX: Determine actual failure status instead of hardcoding 429
        _fail_code = 429
        if hasattr(exc, 'response') and hasattr(exc.response, 'status_code'):
            _fail_code = exc.response.status_code
        log.error("serper_all_retries_exhausted", query=query[:60],
                  error=str(exc), status_code=_fail_code)
        if _fail_code == 429:
            update_circuit_telemetry("serper_429")
        # ── Audit row for failed call ─────────────────────────────────────────
        _async_serper_audit(
            campaign_id       = campaign_id,
            tenant_id         = tenant_id,
            raw_query         = query,
            serper_parameters = payload_dict,
            result_count      = 0,
            status_code       = _fail_code,
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
    sourcing_vector: str = "B2B",
    source_url: str = "",
) -> tuple[str, bool]:
    """Fetch contextual enrichment signals for a domain via Serper.

    V24.1.2: B2B domains receive Places + company profile + hiring queries
    (3 credits). Consumer vectors (B2C/B2B2C/D2C) receive Places + review/
    complaint queries (2 credits). Social domains and non-business suffixes
    (.edu, .gov, .org) are gated and receive no enrichment.

    Args:
        domain:          Root domain string.
        tenant_id:       Tenant UID for usage metering.
        sourcing_vector: Campaign sourcing vector label.
        source_url:      Full original URL (used for domain extraction).

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

    # V24.1.1: Forum prefix detection — "forum.example.com", "talk.site.com", etc.
    if any(cleaned.startswith(p) for p in _FORUM_PREFIXES):
        log.info("enrichment_gated_forum_prefix", domain=domain, prefix=cleaned.split(".")[0])
        return "", False

    # V24.1.1: Forum keyword detection — "gasoccerforum.com" contains "forum"
    if any(kw in cleaned for kw in _FORUM_DOMAIN_KEYWORDS):
        log.info("enrichment_gated_forum_keyword", domain=domain)
        return "", False

    # V24.1.1: Non-business suffixes (.edu, .gov, .org) skip ALL enrichment.
    # Previously only skipped Places; now skips social + hiring queries too.
    # These domains never produce B2B leads and waste 2-3 credits each.
    if any(cleaned.endswith(sfx) for sfx in _NON_BUSINESS_SUFFIXES):
        log.info("enrichment_gated_non_business_suffix", domain=domain)
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

    # V24.1.1 FIX (W3): Consumer vectors get lightweight enrichment instead of
    # being blanket-blocked. B2C leads now receive GMB + review context instead of
    # zero context, reducing the scoring bias against consumer leads.
    _is_consumer = _is_consumer_archetype(sourcing_vector)

    tasks = []
    if _is_consumer:
        # Consumer enrichment: GMB (local presence) + review signals (consumer sentiment)
        # Skip company profile and hiring queries — they produce B2B noise for B2C leads.
        tasks.append(("https://google.serper.dev/places",  {"q": domain, "num": 3}))
        tasks.append(("https://google.serper.dev/search",  {
            "q": f'reviews OR complaints OR "customer experience" "{domain}"', "num": 3,
        }))
        log.info("enrichment_consumer_path", domain=domain, vector=sourcing_vector)
    else:
        # B2B enrichment: Places + company profile + hiring intent (3 queries)
        # NOTE: Non-business suffixes (.edu, .gov, .org) already returned early
        # above, so Places is always relevant here.
        tasks.append(("https://google.serper.dev/places",  {"q": domain, "num": 3}))
        tasks.extend([
            ("https://google.serper.dev/search",  {"q": f'company profile OR social media "{domain}"', "num": 3}),
            ("https://google.serper.dev/search",  {
                "q": f'job openings OR careers "{domain}"',
                "num": 3,
            }),
        ])

    with _cf.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_fetch_parallel, url, body) for url, body in tasks]
        results = []
        for fut in futures:
            try:
                results.append(fut.result(timeout=13))
            except Exception:
                results.append({})

    # Parse results based on enrichment path
    gmb_data = results[0] if results else {}
    hiring_intent = False

    for place in gmb_data.get("places", []):
        context_data.append(
            f"[GMB] Rating: {place.get('rating', 'N/A')}, "
            f"Reviews: {place.get('ratingCount', 'N/A')}, "
            f"Address: {place.get('address', 'N/A')}"
        )

    if _is_consumer:
        # Consumer path: results[1] = review/complaint search
        review_data = results[1] if len(results) > 1 else {}
        for org in review_data.get("organic", []):
            context_data.append(f"[REVIEW] {org.get('snippet', '')}")
    else:
        # B2B path: results[1] = social, results[2] = hiring
        social_data = results[1] if len(results) > 1 else {}
        hiring_data = results[2] if len(results) > 2 else {}

        for org in social_data.get("organic", []):
            context_data.append(f"[SOCIAL] {org.get('snippet', '')}")

        hiring_sigs = [
            "we are hiring", "job description", "apply today",
            "openings", "careers", "looking for", "lakh", "lpa", "fresher",
        ]
        for job in hiring_data.get("organic", []):
            snippet_lc = job.get("snippet", "").lower()
            context_data.append(f"[HIRING] {snippet_lc}")
            if any(sig in snippet_lc for sig in hiring_sigs):
                hiring_intent = True

    # BUG-S2 FIX: Single batched Firestore write for all Serper calls.
    try:
        from google.cloud import firestore as _fs  # type: ignore[import]
        get_db().collection("usage_metrics").document(tenant_id).set(
            {"serper_searches": _fs.Increment(len(tasks))}, merge=True
        )
    except Exception:
        pass

    context_str = " | ".join(context_data)[:3000]
    return context_str, hiring_intent
