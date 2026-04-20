"""
digital-twin-engine / main.py
=============================================================================
POST /api/analyze-website
POST /health

Pipeline (< 8 s total budget):
  1. SSRF Guard          — extract root domain, reject private IPs           (~0 ms)
  2. Serper Acquisition  — site:{domain} + "{domain}" in parallel            (~800 ms)
  3. htmlx Fallback      — lightweight <title>/<meta> scrape if < 50 chars   (~1-2 s, conditional)
  4. Gemini 2.5 Flash    — strict JSON schema via GenerationConfig            (~3-5 s)
  5. Return              — { company: {...}, targets: [...], detected_gl: "" }

Auth: Cloud Run --no-allow-unauthenticated.
      Caller must pass a valid Firebase ID token as Bearer in Authorization.
      Service Account (lead-pipeline-sa) is granted roles/run.invoker.

Error philosophy: hard failures return 500 with a structured JSON body so
the frontend can show its graceful fallback toast (not a blank screen).
=============================================================================
"""

import os
import re
import json
import asyncio
import ipaddress
import concurrent.futures
from urllib.parse import urlparse
from http import HTTPStatus

import httpx
from flask import Flask, request, jsonify
from flask_cors import CORS
from google.cloud import secretmanager
import vertexai
from vertexai.generative_models import GenerativeModel, GenerationConfig
from google.api_core.exceptions import ResourceExhausted
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

# ── Firebase Admin — lightweight ID-token verification ──────────────────────
import firebase_admin
from firebase_admin import auth as firebase_auth, credentials, firestore

app = Flask(__name__)

# Apply strict CORS for production + local dev
CORS(app, 
    origins=["https://lead-sniper-prod.web.app", "http://localhost:5000"],
    methods=["*"],
    allow_headers=["*"],
    supports_credentials=True
)

# =============================================================================
# BOOT INITIALISATION
# =============================================================================

PROJECT_ID = os.environ.get("PROJECT_ID", "sideio-leads-v16")
LOCATION   = os.environ.get("LOCATION", "us-central1")       # Vertex is us-central1

# Vertex AI — must init before any GenerativeModel call
vertexai.init(project=PROJECT_ID, location="us-central1")

# Firebase Admin — init once; uses ADC (no key file needed on Cloud Run)
if not firebase_admin._apps:
    firebase_admin.initialize_app()

# Secret Manager client — shared, lazy secret reads
_sm_client = secretmanager.SecretManagerServiceClient()
_SERPER_SECRET_NAME = f"projects/{PROJECT_ID}/secrets/serper_api_key/versions/latest"

# Module-level secret cache (avoids re-fetching on every request)
_serper_key_cache: str | None = None


def _get_serper_key() -> str:
    global _serper_key_cache
    if _serper_key_cache:
        return _serper_key_cache
    resp = _sm_client.access_secret_version(request={"name": _SERPER_SECRET_NAME})
    _serper_key_cache = resp.payload.data.decode("UTF-8").strip()
    return _serper_key_cache


# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "service": "digital-twin-engine", "version": "1.0.0"}), 200


# =============================================================================
# LAYER 0: AUTH MIDDLEWARE
# Verifies Firebase ID token from Authorization: Bearer <token> header.
# Cloud Run --no-allow-unauthenticated rejects unsigned requests at the
# infrastructure level; this adds an application-level identity check.
# =============================================================================

def _verify_firebase_token(req) -> str | None:
    """
    Returns the uid string if the token is valid, else None.
    Tolerates clock drift up to 5 min (Firebase default).
    """
    auth_header = req.headers.get("Authorization") or req.headers.get("X-Firebase-Auth", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):]
    try:
        decoded = firebase_auth.verify_id_token(token, check_revoked=False)
        return decoded.get("uid")
    except Exception as e:
        print(f"[AUTH] Token verification failed: {e}")
        return None


# =============================================================================
# LAYER 1: SSRF GUARD
# =============================================================================

# CIDR ranges that must never be targeted by outbound requests from this service.
_PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # GCP metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

_VALID_DOMAIN_RE = re.compile(
    r"^(?:[a-zA-Z0-9]"
    r"(?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)"
    r"+[a-zA-Z]{2,}$"
)


def _is_private_or_invalid(hostname: str) -> bool:
    """Return True if the hostname resolves to a private/loopback IP or is malformed."""
    try:
        addr = ipaddress.ip_address(hostname)
        return any(addr in net for net in _PRIVATE_RANGES)
    except ValueError:
        pass  # Not a bare IP — validate as domain

    if not _VALID_DOMAIN_RE.match(hostname):
        return True

    # Reject raw localhost variants
    if hostname.lower() in {"localhost", "metadata", "metadata.google.internal"}:
        return True

    return False


def _extract_root_domain(raw_url: str) -> tuple[str, str]:
    """
    Returns (root_domain, error_message).
    Strips scheme, www., path, query, fragment.
    Returns ('', reason) on any rejection.

    Examples:
        'https://www.acme.com/about?ref=x' → ('acme.com', '')
        '192.168.1.1'                       → ('', 'private IP')
        'http://localhost:8080'             → ('', 'private IP')
        'not a domain'                      → ('', 'invalid domain')
    """
    # Ensure scheme so urlparse can extract netloc
    if not re.match(r"^https?://", raw_url, re.IGNORECASE):
        raw_url = "https://" + raw_url

    try:
        parsed   = urlparse(raw_url)
        hostname = (parsed.hostname or "").lower().strip()
        if not hostname:
            return "", "could not extract hostname"
    except Exception as e:
        return "", f"URL parse error: {e}"

    # Remove www. prefix
    if hostname.startswith("www."):
        hostname = hostname[4:]

    if _is_private_or_invalid(hostname):
        return "", f"rejected hostname: {hostname} (private IP or invalid domain)"

    return hostname, ""


# =============================================================================
# LAYER 2: SERPER ACQUISITION
# Two parallel requests: site-scoped dork + brand query.
# Extracts snippets from organic results AND the Knowledge Graph block.
# =============================================================================

def _serper_search(query: str, api_key: str, num: int = 10) -> dict:
    """Single synchronous Serper call. Returns raw JSON dict."""
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": num}
    resp = httpx.post(
        "https://google.serper.dev/search",
        headers=headers,
        json=payload,
        timeout=6.0  # hard cap — fail fast, let fallback take over
    )
    if resp.status_code == 200:
        return resp.json()
    print(f"[SERPER] HTTP {resp.status_code} for query: {query!r}")
    return {}


def _extract_text_from_serper(results: dict) -> str:
    """
    Pulls text from three Serper blocks:
      • organic[].snippet          — most reliable signal
      • organic[].title            — short but consistent
      • knowledgeGraph.description — best single-sentence company summary
    """
    parts: list[str] = []

    # Knowledge Graph block (highest quality)
    kg = results.get("knowledgeGraph", {})
    if kg.get("description"):
        parts.append(f"[KG] {kg['description']}")
    if kg.get("type"):
        parts.append(f"[KG-TYPE] {kg['type']}")
    for attr_key in ("founded", "headquarters", "ceo", "founders"):
        val = kg.get(attr_key)
        if val:
            parts.append(f"[KG-{attr_key.upper()}] {val}")

    # Organic results
    for r in results.get("organic", [])[:8]:
        title   = r.get("title",   "").strip()
        snippet = r.get("snippet", "").strip()
        if snippet:
            parts.append(snippet)
        elif title:
            parts.append(title)

    return " ".join(parts)


def _run_parallel_serper(root_domain: str) -> str:
    """
    Executes two Serper queries in a ThreadPoolExecutor:
      1. site:{domain}
      2. "{domain}" (brand query — catches third-party descriptions)

    Returns concatenated text string (may be empty on total failure).
    """
    api_key = _get_serper_key()
    queries  = [
        f"site:{root_domain}",
        f'"{root_domain}"',
    ]

    texts: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_serper_search, q, api_key): q for q in queries}
        for future in concurrent.futures.as_completed(futures, timeout=7.0):
            try:
                result = future.result()
                texts.append(_extract_text_from_serper(result))
            except Exception as e:
                print(f"[SERPER] Query failed: {e}")

    return " ".join(t for t in texts if t).strip()


# =============================================================================
# LAYER 3: httpx FALLBACK (meta-only, no DOM parsing)
# Only invoked when Serper text < 50 chars.
# Extracts <title> and <meta name="description"> using simple regex to avoid
# the beautifulsoup4 dependency in this lean service.
# Timeout: 3 s hard cap as specified.
# =============================================================================

_TITLE_RE       = re.compile(r"<title[^>]*>(.*?)</title>",        re.IGNORECASE | re.DOTALL)
_META_DESC_RE   = re.compile(
    r'<meta\s[^>]*name=["\']description["\'][^>]*content=["\']([^"\']{0,500})["\']',
    re.IGNORECASE
)
_META_DESC_RE2  = re.compile(
    r'<meta\s[^>]*content=["\']([^"\']{0,500})["\'][^>]*name=["\']description["\']',
    re.IGNORECASE
)


# WAF tarpit fingerprints shared with orchestrator settings.py
_WAF_FINGERPRINTS = [
    "just a moment",
    "enable javascript and cookies to continue",
    "checking if the site connection is secure",
    "please wait while we check your browser",
    "attention required",
    "cloudflare ray id",
    "datadome",
    "please verify you are human",
    "access denied",
    "403 forbidden",
    "bot detection",
    "please turn javascript on",
]

def _is_waf_page(html: str, status_code: int = 200) -> bool:
    """Returns True if the response looks like a WAF/anti-bot challenge page."""
    if status_code in (403, 429, 503):
        return True
    lowered = html[:8000].lower()
    return any(fp in lowered for fp in _WAF_FINGERPRINTS)


def _httpx_meta_fallback(root_domain: str) -> str:
    """
    Lightweight HEAD+GET to extract just the page title and meta description.
    Uses a 3 s connect+read timeout per spec.
    Returns extracted text or empty string.
    """
    url = f"https://{root_domain}"
    try:
        resp = httpx.get(
            url,
            timeout=httpx.Timeout(connect=2.0, read=3.0, write=3.0, pool=1.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SideioBot/1.0)"},
        )
        if resp.status_code >= 400:
            return ""

        html = resp.text[:30_000]   # only first 30 KB — enough for <head>

        # WAF tarpit check — return sentinel so caller can 422 immediately
        if _is_waf_page(html, resp.status_code):
            print(f"[FALLBACK] WAF detected on {root_domain} (status={resp.status_code})")
            return "__WAF_BLOCKED__"

        parts: list[str] = []

        title_m = _TITLE_RE.search(html)
        if title_m:
            parts.append(title_m.group(1).strip())

        desc_m = _META_DESC_RE.search(html) or _META_DESC_RE2.search(html)
        if desc_m:
            parts.append(desc_m.group(1).strip())

        return " | ".join(p for p in parts if p)
    except Exception as e:
        print(f"[FALLBACK] httpx meta scrape failed for {root_domain}: {e}")
        return ""


# =============================================================================
# LAYER 4: GEMINI 2.5 FLASH — V20 UNIFIED SCHEMA SYNTHESIS
# Merges P6 (company bio + personas) with P7+P8 (product names + market trends)
# into a single generate_content() call.
# The safe_blob is submitted once (≤6000 chars), returning a comprehensive object.
# Post-call: RLHF market_trend_cache lookup overrides LLM-generated trends in Python.
# =============================================================================

# Unified schema: company DNA + predictive campaign trends in one response object
_DT_UNIFIED_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "company_bio": {
            "type": "STRING",
            "description": (
                "1-2 sentences. What does this company actually sell or do? "
                "Be specific. No filler phrases like 'innovative solutions'."
            )
        },
        "target_personas": {
            "type": "ARRAY",
            "description": "Exactly 3 ideal target client personas for this company.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {
                        "type": "STRING",
                        "description": "Short label. E.g. 'E-commerce Brands < 50 employees'."
                    },
                    "description": {
                        "type": "STRING",
                        "description": "1-2 sentences: their pain point and why this company is the right fit."
                    },
                    "location_hint": {
                        "type": "STRING",
                        "description": "Best-guess country/region. Use 'Global' if uncertain."
                    }
                },
                "required": ["name", "description", "location_hint"]
            }
        },
        "products_with_trends": {
            "type": "ARRAY",
            "description": "Up to 3 products/services this company offers, each with a current market trend hook and their unfair advantage.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "product_name":      {"type": "STRING", "description": "Distinct product or service name."},
                    "market_trend_hook": {"type": "STRING", "description": "Current macro-economic trend, pain point, or market shift making this product highly relevant right now."},
                    "unfair_advantage":  {"type": "STRING", "description": "Why this company specifically wins against alternatives for this product."}
                },
                "required": ["product_name", "market_trend_hook", "unfair_advantage"]
            }
        }
    },
    "required": ["company_bio", "target_personas", "products_with_trends"]
}


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=8),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(ResourceExhausted),
)
def _call_gemini_unified(root_domain: str, text_blob: str) -> dict:
    """
    V20: Single Gemini 2.5 Flash call replacing the old P6+P7+P8 chain.
    Accepts safe_blob once; returns company_bio, target_personas, and
    products_with_trends in a single schema-enforced JSON object.
    Hard timeout: 7 s wall-clock via ThreadPoolExecutor.
    """
    safe_blob = text_blob[:6_000]   # ~1500 tokens — well within Flash's context
    prompt = f"""You are a Business Intelligence engine analysing the company at domain: {root_domain}

The following text was collected from their website and search index:
---
{safe_blob}
---

Complete ALL THREE tasks in a single JSON response:

TASK 1 — COMPANY BIO:
Write a company_bio: 1-2 precise sentences describing what they sell/do. Be specific. No filler.

TASK 2 — TARGET PERSONAS:
Identify exactly 3 ideal target_personas — the types of clients this company would pitch to.
For each include: name (short label), description (pain point + why this company solves it), location_hint (country/region; use 'Global' if uncertain).

TASK 3 — PRODUCTS WITH TRENDS:
Identify up to 3 distinct products or services this company offers.
For each, act as a Head of Growth: identify the current macro-economic trend or market shift driving demand (market_trend_hook) and why this company specifically wins against alternatives (unfair_advantage).

Return ONLY valid JSON matching the schema. No markdown, no explanation. Never hallucinate. If text is insufficient, make best inference and flag ambiguity in description fields."""

    model  = GenerativeModel(
        "gemini-2.5-flash",
        system_instruction=(
            "You are a Business Intelligence engine. "
            "Return a precise, factual JSON object. Never hallucinate."
        )
    )
    config = GenerationConfig(
        response_mime_type="application/json",
        response_schema=_DT_UNIFIED_SCHEMA,
        temperature=0.1,
    )

    def _invoke():
        return model.generate_content(prompt, generation_config=config)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_invoke)
        try:
            response = future.result(timeout=7.0)
        except concurrent.futures.TimeoutError:
            raise TimeoutError("Gemini 2.5 Flash unified call timed out (7 s hard cap)")

    return json.loads(response.text)



# =============================================================================
# LAYER 5: MAIN ENDPOINT
# =============================================================================

@app.route("/api/analyze-website", methods=["POST"])
def analyze_website():
    """
    Request body (JSON):
      { "url": "https://acme.com" }

    Response (200):
      {
        "success": true,
        "data": {
          "company": {
            "name":        "acme.com",
            "description": "<company_bio>",
            "value":       ""
          },
          "targets": [
            { "name": "...", "description": "...", "location_hint": "..." },
            ...
          ],
          "detected_gl": ""
        }
      }

    Error response:
      { "success": false, "error": "<reason>", "code": "<slug>" }
    """

    # ── Auth check ────────────────────────────────────────────────────────────
    uid = _verify_firebase_token(request)
    if not uid:
        return jsonify({"success": False, "error": "Unauthorized", "code": "auth_failed"}), 401

    # ── Input extraction ──────────────────────────────────────────────────────
    body = request.get_json(silent=True) or {}
    raw_url = (body.get("url") or "").strip()
    if not raw_url:
        return jsonify({"success": False, "error": "Missing 'url' field", "code": "missing_url"}), 400

    # ── SSRF Guard ────────────────────────────────────────────────────────────
    root_domain, ssrf_error = _extract_root_domain(raw_url)
    if ssrf_error:
        print(f"[SSRF] Rejected URL '{raw_url}': {ssrf_error}")
        return jsonify({"success": False, "error": "Invalid or unsafe URL", "code": "ssrf_rejected"}), 400

    print(f"[DT] Analyzing domain: {root_domain} for uid={uid}")

    # ── Phase 2: Serper Acquisition ───────────────────────────────────────────
    try:
        serper_text = _run_parallel_serper(root_domain)
        print(f"[DT] Serper text length: {len(serper_text)} chars")
    except Exception as e:
        print(f"[DT] Serper acquisition failed: {e}")
        serper_text = ""

    # ── Phase 3: httpx Fallback (if Serper text insufficient) ─────────────────
    combined_text = serper_text
    if len(combined_text.strip()) < 50:
        print(f"[DT] Serper insufficient (<50 chars). Running httpx meta fallback...")
        fallback_text = _httpx_meta_fallback(root_domain)
        if fallback_text == "__WAF_BLOCKED__":
            # httpx fallback hit a WAF challenge page — fail fast with polite error
            print(f"[DT] WAF detected on {root_domain} via httpx fallback — returning WAF_BLOCKED")
            return jsonify({
                "success": False,
                "error": "The target website's security firewall blocked our automated reader.",
                "code":  "WAF_BLOCKED",
            }), 422
        if fallback_text:
            combined_text = f"{combined_text} {fallback_text}".strip()
        print(f"[DT] Post-fallback text length: {len(combined_text)} chars")

    if len(combined_text.strip()) < 20:
        # Absolute minimum — not enough to infer anything meaningful
        return jsonify({
            "success": False,
            "error": "Insufficient public data found for this domain.",
            "code": "insufficient_data"
        }), 422

    # ── Phase 4: V20 Unified Gemini call (replaces P6+P7+P8 two-future chain) ─────
    # Single call submits safe_blob once; returns company_bio + target_personas +
    # products_with_trends in one schema-enforced object.
    # Post-call: RLHF market_trend_cache lookup overrides LLM trends in Python.
    import time as _time
    PHASE4_BUDGET_S = 7.0
    _phase4_start   = _time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            f_unified = pool.submit(_call_gemini_unified, root_domain, combined_text)
            done, not_done = concurrent.futures.wait(
                {f_unified},
                timeout=PHASE4_BUDGET_S,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
            _elapsed = _time.monotonic() - _phase4_start
            print(f"[DT] Phase 4 unified wait: {_elapsed:.2f}s | done={len(done)} | timed_out={len(not_done)}")

            if f_unified not in done:
                raise TimeoutError(
                    f"Unified Gemini synthesis did not complete within "
                    f"{PHASE4_BUDGET_S}s (elapsed={_elapsed:.2f}s)"
                )
            gemini_result = f_unified.result()

    except (TimeoutError, concurrent.futures.TimeoutError):
        return jsonify({
            "success": False,
            "error": "AI synthesis timed out. Please try again.",
            "code": "gemini_timeout"
        }), 504
    except ResourceExhausted:
        return jsonify({
            "success": False,
            "error": "AI quota exceeded. Please retry in a few seconds.",
            "code": "gemini_quota"
        }), 429
    except Exception as e:
        print(f"[DT] Gemini synthesis failed: {e}")
        return jsonify({
            "success": False,
            "error": "AI synthesis failed. Please try again.",
            "code": "gemini_error"
        }), 500

    # ── Phase 4b: RLHF market_trend_cache override (pure Python, zero Gemini) ─
    # For each product returned by the unified call, check the RLHF cache.
    # Cache hits override the LLM-generated trend with the human-vetted version.
    raw_products = gemini_result.get("products_with_trends", [])
    predictive_campaigns: list = []
    try:
        from firebase_admin import firestore as _fs
        _db = _fs.client()
        for prod in raw_products:
            p_str = str(prod.get("product_name", "")).strip()
            if not p_str:
                continue
            doc_id = "".join(c for c in p_str.lower() if c.isalnum() or c in ["-", "_"])[:100]
            if doc_id:
                cached = _db.collection("market_trend_cache").document(doc_id).get()
                if cached.exists:
                    c_data = cached.to_dict()
                    predictive_campaigns.append({
                        "product_name":      p_str,
                        "market_trend_hook": c_data.get("market_trend_hook", prod.get("market_trend_hook", "")),
                        "unfair_advantage":  c_data.get("unfair_advantage",  prod.get("unfair_advantage",  "")),
                    })
                    print(f"[RLHF] Cache override applied for '{p_str}': {doc_id}")
                    continue
            # No cache hit — use LLM-generated trend as-is
            predictive_campaigns.append(prod)
        print(f"[DT] Predictive campaigns resolved: {len(predictive_campaigns)} (after RLHF override)")
    except Exception as ce:
        print(f"[DT] RLHF cache override failed (non-fatal): {ce}")
        predictive_campaigns = raw_products  # fallback: use raw LLM output



    # ── Phase 5: Shape response for frontend (View C) ─────────────────────────
    company_bio = gemini_result.get("company_bio", "")
    personas    = gemini_result.get("target_personas", [])

    # Detect geo from first persona's location_hint (best-effort)
    detected_gl = ""
    if personas:
        loc_hint = (personas[0].get("location_hint") or "").lower()
        _GL_MAP = {
            "india": "in", "usa": "us", "united states": "us",
            "uk": "uk", "united kingdom": "uk", "canada": "ca",
            "australia": "au", "germany": "de", "singapore": "sg",
            "uae": "ae", "dubai": "ae",
        }
        for kw, gl in _GL_MAP.items():
            if kw in loc_hint:
                detected_gl = gl
                break

    # Normalise personas into the frontend schema
    # Frontend expects: { name, description } — location_hint is bonus
    normalised_targets = []
    for p in personas[:3]:
        normalised_targets.append({
            "name":         p.get("name", "Target Persona"),
            "description":  p.get("description", ""),
            "location_hint": p.get("location_hint", "Global"),
        })

    # Pad to exactly 3 if Gemini returned fewer (schema should prevent this)
    while len(normalised_targets) < 3:
        normalised_targets.append({
            "name": f"Target Persona {len(normalised_targets) + 1}",
            "description": "No additional persona data was extracted.",
            "location_hint": "Global",
        })

    response_payload = {
        "success": True,
        "data": {
            "company": {
                "name":        root_domain,
                "description": company_bio,
                "value":       "",               # populated by frontend edit if needed
            },
            "targets":     normalised_targets,
            "recommended_campaigns": predictive_campaigns,
            "detected_gl": detected_gl,
        }
    }

    print(f"[DT] Success for {root_domain}: {len(normalised_targets)} personas, gl={detected_gl!r}")
    return jsonify(response_payload), 200


# =============================================================================
# LOCAL DEV RUNNER
# =============================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
