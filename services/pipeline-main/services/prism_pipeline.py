"""
Pipeline-Main V23 — PrismPipeline Service Module (SF-002 Fix).

EXTRACTION RATIONALE (2026-04-18):
  The original dispatch.py used importlib.exec_module() to load the entire
  main.py monolith (3,185 lines) at runtime just to access PrismPipeline.
  This caused SF-002: the monolith's module-scope code (flask app creation,
  cipher_suite = Fernet(KEY), @app.route decorators) executed inside the
  exec_module() call, with unpredictable side-effects in the V23 container.

  fix: This module extracts the 5 PRISM classes and their supporting
  constants/helpers verbatim from main.py (lines 1585-2297), rewriting
  only the import statements and db references to use V23 lazy accessors.

  Zero import-time side effects:
  - No Flask app creation
  - No gRPC client construction (db_client passed in by caller)
  - No vertexai.init / Secret Manager calls
  - No Fernet/ENCRYPTION_KEY dependency

Classes exported:
  PrismPipeline          — main entry point, instantiate once per /dispatch call
  OperatingModeRouter    — classifies URLs into WalledGarden/GeneralDomain/B2B2C
  WalledGardenHook       — Serper snippet triangulation for social/UGC URLs
  GeneralDomainHook      — httpx DOM scrape for open-web B2B domains
  B2B2CIntermediaryFinder — finds local distributors/resellers for B2B2C mode
"""
from __future__ import annotations

import concurrent.futures
import random
import re
from urllib.parse import urlparse

import httpx                         # type: ignore[import]
from bs4 import BeautifulSoup        # type: ignore[import]
from google.cloud import firestore   # type: ignore[import]

from core.logging import get_logger  # type: ignore[import]
from services.serper_service import extract_root_domain  # type: ignore[import]

log = get_logger("pipeline.prism")

# ---------------------------------------------------------------------------
# Domain constants (verbatim from main.py lines 1585-1607)
# ---------------------------------------------------------------------------

WALLED_GARDEN_DOMAINS: set[str] = {
    "reddit.com", "facebook.com", "instagram.com",
    "x.com", "twitter.com", "quora.com", "youtube.com", "team-bhp.com",
    "tiktok.com", "pinterest.com", "snapchat.com", "threads.net",
    # NOTE: "linkedin.com" is intentionally omitted.
    # linkedin.com/company/ URLs are strict B2B and must undergo full enrichment
    # via GeneralDomainHook. The OperatingModeRouter.route() applies a
    # path-level check to send /company/ URLs to GeneralDomain.
}

_WAF_FINGERPRINTS = [
    "just a moment", "attention required!", "cloudflare ray id",
    "access denied", "403 forbidden", "please verify you are human",
    "enable javascript and cookies to continue",
    "checking if the site connection is secure",
]

_TECH_SIGNATURES: dict[str, str] = {
    "wordpress":        "wp-content",
    "shopify":          "cdn.shopify.com",
    "stripe":           "js.stripe.com",
    "react":            "react-root",
    "hubspot":          "js.hs-scripts.com",
    "salesforce":       "force.com",
    "google analytics": "google-analytics.com",
    "segment":          "cdn.segment.com",
    "intercom":         "widget.intercom.io",
    "crisp":            "crisp.chat",
    "zendesk":          "zopim.com",
    "drift":            "drift.com/drift-frame",
}


# ---------------------------------------------------------------------------
# Module-level helpers (verbatim from main.py)
# ---------------------------------------------------------------------------

def _is_waf_blocked(html_or_text: str) -> bool:
    """Returns True if the response looks like a WAF/bot-challenge page."""
    lowered = html_or_text.lower()
    return any(fp in lowered for fp in _WAF_FINGERPRINTS)


def _extract_tech_stack(html_blob: str) -> list[str]:
    lowered = html_blob.lower()
    return [name for name, sig in _TECH_SIGNATURES.items() if sig in lowered]


def _persona_match_score(text: str, persona_summary: str) -> int:
    import re
    persona_tokens = set(re.findall(r"\b\w{4,}\b", persona_summary.lower()))
    text_tokens    = set(re.findall(r"\b\w{4,}\b", text.lower()[:8000]))
    if not persona_tokens:
        return 5
    overlap = len(persona_tokens & text_tokens)
    return min(10, int((overlap / max(len(persona_tokens), 1)) * 20))


def _safe_truncate(text: str, max_bytes: int = 100_000) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# LAYER 0 — Operating Mode Router
# (verbatim from main.py lines 1614-1697)
# ---------------------------------------------------------------------------

class OperatingModeRouter:
    """
    Classifies a candidate URL into one of three processing modes:
      • 'WalledGarden'  — social/UGC domains; snippet-based analysis
      • 'GeneralDomain' — open web domains; httpx DOM scrape
      • 'B2B2C'         — consumer-intent URLs requiring intermediary search

    Classification logic (priority order):
      1. linkedin.com/company/ → GeneralDomain (strict B2B)
      2. Root domain in WALLED_GARDEN_DOMAINS → WalledGarden
      3. B2B2C persona signal AND B2B2C URL signal → B2B2C
      4. Default → GeneralDomain
    """

    _B2B2C_URL_SIGNALS   = {"review", "compare", "best", "near-me", "near+me",
                             "recommendation", "alternative", "vs", "find"}
    _B2B2C_PERSONA_FLAGS = {
        "consumer", "individual", "student", "patient", "retail",
        "end user", "end-user", "buyer", "shopper", "household",
        "b2b2c", "distributor", "reseller", "channel partner",
    }

    def __init__(self, target_personas: list[dict]):
        self._personas  = target_personas or []
        self._has_b2b2c = self._detect_b2b2c_campaign()

    def _detect_b2b2c_campaign(self) -> bool:
        for persona in self._personas:
            desc = (persona.get("description", "") + " " + persona.get("name", "")).lower()
            if any(flag in desc for flag in self._B2B2C_PERSONA_FLAGS):
                return True
        return False

    def route(self, url: str) -> str:
        root_domain = extract_root_domain(url)
        url_lower   = url.lower()

        # linkedin.com/company/ → strict B2B GeneralDomain
        if "linkedin.com" in root_domain and "/company/" in url_lower:
            log.info("prism_route_linkedin_company", url=url[:80], mode="GeneralDomain")
            return "GeneralDomain"

        # Priority 1: walled garden check
        if "linkedin.com" in root_domain or any(root_domain.endswith(wg) for wg in WALLED_GARDEN_DOMAINS):
            return "WalledGarden"

        # Priority 2: B2B2C
        if self._has_b2b2c and any(sig in url_lower for sig in self._B2B2C_URL_SIGNALS):
            return "B2B2C"

        return "GeneralDomain"

    def summarise_personas(self) -> str:
        if not self._personas:
            return "No target personas defined."
        lines = []
        for i, p in enumerate(self._personas[:3], 1):
            lines.append(
                f"{i}. {p.get('name', 'Unknown')} — {p.get('description', '')} "
                f"[{p.get('location_hint', 'Global')}]"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# LAYER 1A — WalledGarden Hook
# (verbatim from main.py lines 1707-1863)
# ---------------------------------------------------------------------------

class WalledGardenHook:
    """
    Processes walled-garden / social URLs via Serper snippet triangulation.
    3-query parallel Serper → concatenate all organic + KG snippets.
    Applies SHADOW_LEARNER_THIN_PAYLOAD marker if text < 500 chars.
    """

    def __init__(self, db_client, serper_key: str):
        self._db         = db_client
        self._serper_key = serper_key

    def _build_queries(self, url: str, root_domain: str, persona_summary: str) -> list[str]:
        """Build Serper triangulation queries for a social URL.

        V27.5: Single high-precision ``site:`` query (was 3 — dual bare-domain
        queries doubled Serper cost and pulled cold SERP noise). Strip numbered
        list labels and campaign titles from path/persona fragments.
        """
        import re as _re
        parsed = urlparse(url)
        raw_parts = [
            w for w in parsed.path.replace("-", " ").replace("_", " ").split("/")
            if len(w) > 2 and not w.isdigit()
        ]
        # Drop reddit path noise tokens that are not identity
        _skip = {"comments", "comment", "status", "posts", "post", "share", "r"}
        path_slug = " ".join(w for w in raw_parts if w.lower() not in _skip)[:80]
        # Strip "1. Seed Investment" style labels from path material
        path_slug = _re.sub(
            r"(?i)\b\d{1,2}[.)]\s+[A-Za-z][\w /&-]{2,40}", " ", path_slug
        )
        path_slug = _re.sub(r"\s+", " ", path_slug).strip()

        # Prefer site: only — bare domain" twins were pure credit waste in prod logs
        if path_slug:
            return [f"site:{root_domain} {path_slug}".strip()]
        # Fallback: site:domain alone (still 1 credit vs 3)
        return [f"site:{root_domain}".strip()]

    def _run_serper(self, query: str) -> dict:
        """Execute a Serper search via the centralized service (circuit breaker + audit)."""
        try:
            from services.serper_service import search_serper  # type: ignore[import]
            results = search_serper(query, residual=True)
            # search_serper returns organic list; wrap in dict for _extract_snippets
            return {"organic": results} if results else {}
        except Exception as e:
            log.warning("walled_garden_serper_failed", query=query[:60], error=str(e))
        return {}

    def _extract_snippets(self, serper_result: dict) -> str:
        parts: list[str] = []
        kg = serper_result.get("knowledgeGraph", {})
        if kg.get("description"):
            parts.append(f"[KG] {kg['description']}")
        for r in serper_result.get("organic", [])[:8]:
            snippet = r.get("snippet", "").strip()
            title   = r.get("title",   "").strip()
            if snippet:
                parts.append(snippet)
            elif title:
                parts.append(title)
        return " ".join(parts)

    def fetch(self, url: str, root_domain: str, persona_summary: str, tenant_id: str) -> dict:
        """Returns: { text, tech_stack, emails, phones, mode, fallback_used }"""
        cache_key = url.replace("/", "_")
        cache_ref = self._db.collection("scraped_cache").document(cache_key)

        # Cache read — V25.3.0 Credit Guard
        # Only skip Serper queries when cached text is substantial (>= 500 chars).
        # signal_harvest may have written full content to a sha256-keyed doc that
        # doesn't match the PRISM-native cache_key. If PRISM's own cache has thin
        # text from a prior WalledGarden run, fall through to Serper for a richer
        # triangulation rather than returning a low-quality snippet.
        try:
            cached = cache_ref.get()
            if cached.exists:
                c = cached.to_dict()
                cached_text = c.get("text", "")
                if cached_text and len(cached_text) >= 500:
                    log.info("prism_walled_garden_cache_hit",
                             url=url[:80], chars=len(cached_text),
                             source=c.get("source", "unknown"))
                    return {
                        "text":         cached_text,
                        "tech_stack":   c.get("tech_stack", ["Social Platform Snippet"]),
                        "emails":       c.get("emails", []),
                        "phones":       c.get("phones", []),
                        "mode":         "WalledGarden",
                        "fallback_used": False,
                        "company_name": "",
                    }
                elif cached_text:
                    log.info("walled_garden_cache_thin_skip",
                             url=url[:80], chars=len(cached_text),
                             note="Cached text below 500-char threshold; proceeding to Serper.")
        except Exception as ce:
            log.warning("walled_garden_cache_read_error", url=url[:80], error=str(ce))

        # BUG-P1 FIX: as_completed(timeout=7.0) raises TimeoutError on the iterator
        # if ANY single future is not done within 7s of the FIRST completion.
        # This silently abandoned any futures not yet complete — no text collected.
        # Fix: iterate without outer timeout; each future already has a 6s httpx
        # timeout internally. Add individual fut.result(timeout=8) per future.
        queries = self._build_queries(url, root_domain, persona_summary)
        all_texts: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(self._run_serper, q): q for q in queries}
            for future in concurrent.futures.as_completed(futures):
                try:
                    extracted = self._extract_snippets(future.result(timeout=8))
                    if extracted:
                        all_texts.append(extracted)
                except Exception as fe:
                    log.warning("walled_garden_triangulation_failed", error=str(fe))

        combined_text = " ".join(all_texts).strip()

        # Thin payload → shadow learner marker
        if len(combined_text) < 500:
            combined_text = f"[SHADOW_LEARNER_THIN_PAYLOAD] {combined_text}"
            log.info("walled_garden_thin_payload", url=url[:80], chars=len(combined_text))

        # Cache write
        # Postmortem Fix #12: include expire_at so the Firestore TTL policy
        # (once enabled in GCP Console on scraped_cache.expire_at) auto-deletes
        # stale entries. Without TTL: 547k permanent docs accumulate over 12 months.
        if combined_text:
            try:
                import datetime as _dt
                _expire = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=30)
                cache_ref.set({
                    "url":        url,
                    "text":       _safe_truncate(combined_text),
                    "source":     "walled_garden_triangulation",
                    "tech_stack": ["Social Platform Snippet"],
                    "emails":     [],
                    "phones":     [],
                    "expire_at":  _expire,   # TTL field — enable in Firestore Console
                }, merge=True)
            except Exception as cw:
                log.warning("walled_garden_cache_write_failed", url=url[:80], error=str(cw))


        # Serper spend telemetry
        try:
            self._db.collection("usage_metrics").document(tenant_id).set(
                {"serper_searches": firestore.Increment(len(queries))}, merge=True
            )
        except Exception:
            pass

        return {
            "text":         combined_text,
            "tech_stack":   ["Social Platform Snippet"],
            "emails":       [],
            "phones":       [],
            "mode":         "WalledGarden",
            "fallback_used": False,
            "company_name": "",
        }


# ---------------------------------------------------------------------------
# LAYER 1B — General Domain Hook
# (verbatim from main.py lines 1913-2084)
# ---------------------------------------------------------------------------

class GeneralDomainHook:
    """
    Processes open-web B2B domains via httpx DOM scrape.
    WAF-detected pages fall back to WalledGardenHook snippet path.
    """

    def __init__(self, db_client, serper_key: str):
        self._db      = db_client
        self._wg_hook = WalledGardenHook(db_client, serper_key)

    def fetch(self, url: str, root_domain: str, persona_summary: str, tenant_id: str) -> dict:
        """Returns: { text, tech_stack, emails, phones, mode, fallback_used, persona_match_score }"""
        cache_key = url.replace("/", "_")
        cache_ref = self._db.collection("scraped_cache").document(cache_key)

        # Cache read (skip serper_snippet sources — those are thin)
        try:
            cached = cache_ref.get()
            if cached.exists:
                c = cached.to_dict()
                if c.get("text") and c.get("source") != "serper_snippet":
                    log.info("general_domain_cache_hit", url=url[:80])
                    return {
                        "text":               c["text"],
                        "tech_stack":         c.get("tech_stack", []),
                        "emails":             c.get("emails", []),
                        "phones":             c.get("phones", []),
                        "mode":               "GeneralDomain",
                        "fallback_used":      False,
                        "persona_match_score": _persona_match_score(c["text"], persona_summary),
                    }
        except Exception as ce:
            log.warning("general_domain_cache_read_error", url=url[:80], error=str(ce))

        text: str       = ""
        tech_stack: list[str] = []
        emails: list[str]     = []
        phones: list[str]     = []

        try:
            resp = httpx.get(
                url,
                timeout=httpx.Timeout(connect=4.0, read=10.0, write=10.0, pool=1.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SideioBot/1.0; +https://sideio.com)"},
            )
            raw_html = resp.text

            # WAF detection → WalledGarden fallback
            if _is_waf_blocked(raw_html) or resp.status_code in (403, 429, 503):
                log.info("general_domain_waf_fallback", url=url[:80], status=resp.status_code)
                wg_result = self._wg_hook.fetch(url, root_domain, persona_summary, tenant_id)
                wg_result["fallback_used"]       = True
                wg_result["mode"]                = "GeneralDomain→WalledGardenFallback"
                wg_result["persona_match_score"] = _persona_match_score(
                    wg_result.get("text", ""), persona_summary
                )
                return wg_result

            soup       = BeautifulSoup(raw_html, "html.parser")
            tech_stack = _extract_tech_stack(raw_html.lower())
            emails     = list({
                a["href"].replace("mailto:", "").split("?")[0].strip()
                for a in soup.find_all("a", href=True)
                if a["href"].startswith("mailto:")
            })[:5]
            phones = list({
                a["href"].replace("tel:", "").strip()
                for a in soup.find_all("a", href=True)
                if a["href"].startswith("tel:")
            })[:3]

            semantic_zones = soup.find_all(["main", "article", "section", "header"])
            if semantic_zones:
                text = " ".join(zone.get_text(separator=" ", strip=True) for zone in semantic_zones)
            else:
                text = soup.get_text(separator=" ", strip=True)

            if len(text) < 150:
                log.info("general_domain_thin_page_fallback", url=url[:80], chars=len(text))
                wg_result = self._wg_hook.fetch(url, root_domain, persona_summary, tenant_id)
                wg_result["fallback_used"]       = True
                wg_result["mode"]                = "GeneralDomain→WalledGardenFallback"
                wg_result["persona_match_score"] = _persona_match_score(
                    wg_result.get("text", ""), persona_summary
                )
                return wg_result

        except httpx.TimeoutException:
            log.warning("general_domain_httpx_timeout", url=url[:80])
            wg_result = self._wg_hook.fetch(url, root_domain, persona_summary, tenant_id)
            wg_result["fallback_used"]       = True
            wg_result["mode"]                = "GeneralDomain→WalledGardenFallback(Timeout)"
            wg_result["persona_match_score"] = _persona_match_score(
                wg_result.get("text", ""), persona_summary
            )
            return wg_result
        except Exception as e:
            # BUG-P2 FIX: Previously returned empty text dict immediately.
            # Empty text causes dispatch.py to defer to scraper-heavy.
            # Better: attempt WalledGarden snippet triangulation first —
            # even a thin snippet gives Gemini something to score.
            log.warning("general_domain_scrape_error_wg_fallback",
                        url=url[:80], error=str(e))
            try:
                wg_result = self._wg_hook.fetch(url, root_domain, persona_summary, tenant_id)
                wg_result["fallback_used"]       = True
                wg_result["mode"]                = "GeneralDomain→WalledGardenFallback(Error)"
                wg_result["persona_match_score"] = _persona_match_score(
                    wg_result.get("text", ""), persona_summary
                )
                return wg_result
            except Exception as wg_e:
                log.warning("general_domain_wg_fallback_failed", url=url[:80], error=str(wg_e))
                return {
                    "text": "", "tech_stack": [], "emails": [], "phones": [],
                    "mode": "GeneralDomain", "fallback_used": False,
                    "persona_match_score": 0,
                }

        # Cache write
        try:
            cache_ref.set({
                "url":        url,
                "text":       _safe_truncate(text),
                "source":     "general_domain_httpx",
                "tech_stack": tech_stack,
                "emails":     emails,
                "phones":     phones,
            }, merge=True)
        except Exception as cw:
            log.warning("general_domain_cache_write_failed", url=url[:80], error=str(cw))

        pms = _persona_match_score(text, persona_summary)
        log.info("general_domain_scraped",
                 url=url[:80], chars=len(text), tech_stack=tech_stack, persona_match=pms)

        return {
            "text":               _safe_truncate(text),
            "tech_stack":         tech_stack,
            "emails":             emails,
            "phones":             phones,
            "mode":               "GeneralDomain",
            "fallback_used":      False,
            "persona_match_score": pms,
        }


# ---------------------------------------------------------------------------
# LAYER 1C — B2B2C Intermediary Finder
# (verbatim from main.py lines 2093-2234)
# ---------------------------------------------------------------------------

class B2B2CIntermediaryFinder:
    """
    B2B2C Bridge: finds local distributor/reseller/channel partners
    who carry the product/service relevant to the consumer-intent URL.
    """

    _GL_MAP = {
        "india": ("India", "in"), "usa": ("USA", "us"),
        "united states": ("USA", "us"), "uk": ("UK", "gb"),
        "united kingdom": ("UK", "gb"), "canada": ("Canada", "ca"),
        "australia": ("Australia", "au"), "germany": ("Germany", "de"),
        "singapore": ("Singapore", "sg"), "uae": ("UAE", "ae"),
        "dubai": ("UAE", "ae"), "global": ("", ""),
    }

    def __init__(self, db_client, serper_key: str):
        self._db         = db_client
        self._serper_key = serper_key

    def _serper_search(self, query: str, gl: str | None = None) -> list[dict]:
        """Execute a Serper search via the centralized service (circuit breaker + audit)."""
        try:
            from services.serper_service import search_serper  # type: ignore[import]
            results = search_serper(query, gl=gl, residual=True)
            return results if results else []
        except Exception as e:
            log.warning("b2b2c_serper_failed", query=query[:60], error=str(e))
        return []

    def _derive_geo(self, personas: list[dict]) -> tuple[str, str]:
        for persona in personas:
            hint = persona.get("location_hint", "Global").lower()
            if hint and hint != "global":
                for kw, vals in self._GL_MAP.items():
                    if kw in hint:
                        return vals
        return "", ""

    def find_intermediaries(
        self,
        consumer_url: str,
        root_domain:  str,
        personas:     list[dict],
        persona_summary: str,
        tenant_id:    str,
    ) -> dict:
        location_str, gl = self._derive_geo(personas)
        parsed     = urlparse(consumer_url)
        url_words  = " ".join(
            w for w in parsed.path.replace("-", " ").replace("_", " ").split("/")
            if len(w) > 2
        )[:60]
        persona_category = (personas[0].get("name", "") if personas else "")[:50]
        product_category = (url_words or persona_category or root_domain)[:80]

        geo_suffix = f" {location_str}" if location_str else ""
        queries = [
            f'"{product_category}" distributor reseller{geo_suffix} -site:alibaba.com',
            f'"{product_category}" channel partner stockist{geo_suffix} B2B',
        ]

        log.info("b2b2c_finding_intermediaries",
                 category=product_category, geo=location_str, gl=gl)

        # BUG-P1 FIX (same as WalledGarden): drop outer as_completed timeout.
        # Individual futures already have 6s httpx timeout. Use per-future
        # result(timeout=8) to bound each individual call.
        all_snippets: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self._serper_search, q, gl or None) for q in queries]
            for future in concurrent.futures.as_completed(futures):
                try:
                    for r in future.result(timeout=8)[:6]:
                        snippet = r.get("snippet", "")
                        if snippet:
                            all_snippets.append(
                                f"[INTERMEDIARY] {r.get('title', '')} — {r.get('link', '')}\n{snippet}"
                            )
                except Exception as fe:
                    log.warning("b2b2c_future_error", error=str(fe))

        combined_text = "\n\n".join(all_snippets) if all_snippets else ""
        context_header = (
            f"[B2B2C BRIDGE MODE]\n"
            f"Consumer Intent Source: {consumer_url}\n"
            f"Product/Service Category: {product_category}\n"
            f"Target Geography: {location_str or 'Global'}\n"
            f"Campaign Persona: {persona_summary[:200]}\n\n"
            f"The following are local distributors, resellers, or channel partners "
            f"who can reach the consumer segment described above. "
            f"Score each as a B2B lead for the vendor (NOT the consumer):\n\n"
            f"{combined_text}"
        )

        try:
            self._db.collection("usage_metrics").document(tenant_id).set(
                {"serper_searches": firestore.Increment(len(queries))}, merge=True
            )
        except Exception:
            pass

        return {
            "text":               _safe_truncate(context_header),
            "tech_stack":         ["B2B2C Intermediary Search"],
            "emails":             [],
            "phones":             [],
            "mode":               "B2B2C",
            "fallback_used":      False,
            "persona_match_score": 5,
            "company_name":       "",
        }


# ---------------------------------------------------------------------------
# THE PRISM PIPELINE — Main Orchestrator
# (verbatim from main.py lines 2243-2296)
# ---------------------------------------------------------------------------

class PrismPipeline:
    """
    Composes OperatingModeRouter + the three hooks into a single
    callable that dispatch() uses per URL.

    Usage:
        prism = PrismPipeline(campaign, get_db(), serper_key)
        hook_result = prism.process_url(url, tenant_id)
        text        = hook_result["text"]
        tech_stack  = hook_result["tech_stack"]

    Never raises — all exceptions are caught and returned as empty text.
    """

    def __init__(self, campaign_doc: dict, db_client, serper_key: str):
        target_personas      = campaign_doc.get("target_personas", [])
        self._router         = OperatingModeRouter(target_personas)
        self._personas       = target_personas
        self._wg_hook        = WalledGardenHook(db_client, serper_key)
        self._gd_hook        = GeneralDomainHook(db_client, serper_key)
        self._b2c_hook       = B2B2CIntermediaryFinder(db_client, serper_key)
        self._persona_summary = self._router.summarise_personas()

    def process_url(self, url: str, tenant_id: str) -> dict:
        """
        Routes the URL to the correct hook and returns a unified result.
        Never raises.
        """
        root_domain = extract_root_domain(url)
        mode        = self._router.route(url)

        log.info("prism_process_url", url=url[:80], domain=root_domain, mode=mode)

        try:
            if mode == "WalledGarden":
                return self._wg_hook.fetch(url, root_domain, self._persona_summary, tenant_id)
            elif mode == "B2B2C":
                return self._b2c_hook.find_intermediaries(
                    url, root_domain, self._personas, self._persona_summary, tenant_id
                )
            else:  # GeneralDomain
                return self._gd_hook.fetch(url, root_domain, self._persona_summary, tenant_id)
        except Exception as e:
            log.error("prism_unhandled_exception", url=url[:80], mode=mode,
                      error=str(e), exc_info=True)
            return {
                "text": "", "tech_stack": [], "emails": [], "phones": [],
                "mode": mode, "fallback_used": False,
                "persona_match_score": 0,
            }
