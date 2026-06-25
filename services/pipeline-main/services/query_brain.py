"""
Pipeline-main — Query Brain service.

Extracted from ``main.py:generate_smart_query()``.

V21 Hybrid Starter Motor — routes between:
  STATISTICAL  (Confidence >= threshold): constructs Serper queries locally
               from top-N BigQuery N-grams. Zero Gemini calls.
  GEMINI_FALLBACK (Confidence < threshold): Unified Gemini prompt (legacy V20).

V23 changes:
  - All gRPC clients via lazy accessors: ``get_db()``, ``get_bq_client()``.
  - ``call_gemini_2_5()`` imported from ``services.gemini_service`` — never
    instantiates a model at module scope.
  - Structured JSON logging throughout.
"""
from __future__ import annotations

import concurrent.futures
from typing import Optional

from core.logging import get_logger   # type: ignore[import]
from core.clients import get_db, get_bq_client  # type: ignore[import]
from core.config import PROJECT_ID  # type: ignore[import]

log = get_logger("pipeline.query_brain")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VECTOR_PLATFORM_MAP: dict[str, list[str]] = {}

# FIX (2026-06-20): Removed hardcoded "B2B" from schema description.
# The word "B2B" in the schema primed Gemini to hallucinate corporate
# pain points even for B2C/Real Estate campaigns.
_QUERY_BRAIN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "historical_phrases": {
            "type":        "ARRAY",
            "description": "Up to 3 short trend phrases from historical lead pain_points. Return empty array if no data provided.",
            "items":       {"type": "STRING"},
        },
        "symptom_dorks": {
            "type":        "ARRAY",
            "description": "Exactly 3 Google Search operator strings.",
            "items":       {"type": "STRING"},
        },
        "translated_queries": {
            "type":        "ARRAY",
            "description": "Exactly 3 natural-language platform-native queries.",
            "items":       {"type": "STRING"},
        },
    },
    "required": ["historical_phrases", "symptom_dorks", "translated_queries"],
}

_DEFAULT_BLACKLIST = (
    "-wiki -jobs -careers -investors -support -\"login\" "
    "-www.zoominfo.com -www.ibm.com -www.amazon.com"
)

# ---------------------------------------------------------------------------
# Consumer Archetype Detection (V23 Dynamic Refactor)
# ---------------------------------------------------------------------------
# FIX (2026-06-21): Replaced brittle exact-string set
# ({"b2c", "real estate", "b2c2b", "property"}) with archetype-based check.
# The canonical archetypes (B2C, B2B2C, D2C) are defined in
# orchestrator.core.helpers. This module re-exports a standalone function
# so downstream pipeline modules can import it without cross-service deps.
#
# The actual industry context (Real Estate, Dental, SaaS) is passed via
# the campaign's effective_bio and keywords — NOT inferred from the vector.
# ---------------------------------------------------------------------------

_CONSUMER_ARCHETYPES: frozenset = frozenset({"B2C", "B2B2C", "D2C"})


def _is_consumer_archetype(vector: str) -> bool:
    """Return True if *vector* is a consumer-facing business archetype.

    Handles new archetypes (B2C, B2B2C, D2C) and guarantees backwards
    compatibility — legacy values (Classic B2B, Social/Forum Listening,
    Review Hijacking, Maps/GMB Targeting) all return False.

    This is the single source of truth for consumer routing across the
    entire pipeline-main service. Imported by produce.py, dispatch.py,
    and serper_service.py.
    """
    return (vector or "").upper().strip() in _CONSUMER_ARCHETYPES


# ---------------------------------------------------------------------------
# Scoped Context Class
# ---------------------------------------------------------------------------

class CampaignQueryContext:
    """Scoped query construction context for a single evaluated campaign instance.
    
    Enforces strict campaign-level isolation and context purification to prevent
    leakage/cross-contamination of database attributes (target_audience, pain_points,
    intents) across distinct campaign loops.
    """
    def __init__(
        self,
        campaign_id: Optional[str],
        tenant_id: str,
        user_keywords: list[str],
        bio: str,
        sourcing_vector: Optional[str] = None,
        persona_category: Optional[str] = None,
        targeting_signals: Optional[list] = None,
    ):
        self.campaign_id = campaign_id
        self.tenant_id = tenant_id
        self.target_audience = list(user_keywords) if user_keywords else []
        self.bio = bio or ""
        self.sourcing_vector = sourcing_vector
        self.persona_category = persona_category
        self.targeting_signals = list(targeting_signals) if targeting_signals else []
        
        # Scoped dynamic arrays
        self.pain_points: list[str] = []
        self.intents: list[str] = []
        self.neg_domains: list[str] = []
        self.neg_title_frags: list[str] = []
        self.symptom_dorks: list[str] = []
        self.has_local_history: bool = False


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def generate_smart_query(
    user_keywords: list[str],
    tenant_id: str,
    bio: str,
    sourcing_vector: Optional[str] = None,
    persona_category: Optional[str] = None,
    targeting_signals: Optional[list] = None,
    campaign_id: Optional[str] = None,
) -> list[str]:
    """Generate Serper query strings via statistical router or Gemini fallback.

    Args:
        user_keywords:      List of campaign keyword strings.
        tenant_id:          Tenant UID for BQ scoping.
        bio:                Effective campaign bio.
        sourcing_vector:    Campaign sourcing archetype ("B2B", "B2C", etc.).
        persona_category:   Persona category for BQ intent confidence query.
        targeting_signals:  List of Persona targeting signal strings.  Any
                            entry starting with "NOT " (case-insensitive) is
                            parsed into a Serper exclusion operator.

    Returns:
        List of ready-to-use Serper query strings (may be empty on error).
    """
    from services.neg_shield import fetch_neg_shield  # type: ignore[import]
    from services.gemini_service import call_gemini_2_5  # type: ignore[import]
    import json

    # Instantiate the campaign scoped context to ensure absolute data boundary isolation.
    ctx = CampaignQueryContext(
        campaign_id=campaign_id,
        tenant_id=tenant_id,
        user_keywords=user_keywords,
        bio=bio,
        sourcing_vector=sourcing_vector,
        persona_category=persona_category,
        targeting_signals=targeting_signals,
    )

    # ── Step 1: RLHF history (Firestore read) ─────────────────────────────────
    # FIX (2026-06-20): Prevent global tenant fallback leakage.
    # Without campaign_id scoping, the query returns pain_points from ALL tenant
    # campaigns — including B2B SaaS. Those B2B pain points ("Weak brand story",
    # "unclear positioning") then leak into Real Estate queries via historical_str.
    #
    # Policy:
    #   - campaign_id present → scope to that campaign only (safe)
    #   - campaign_id missing + consumer vector → SKIP entirely (no tenant-wide)
    #   - campaign_id missing + B2B vector → allow tenant-wide (acceptable)
    _is_consumer_ctx = _is_consumer_archetype(ctx.sourcing_vector)
    _skip_rlhf = (not ctx.campaign_id) and _is_consumer_ctx

    if _skip_rlhf:
        log.info("query_brain_rlhf_skipped_consumer_no_campaign_id",
                 vector=ctx.sourcing_vector, tenant_id=ctx.tenant_id[:10],
                 note="Skipping tenant-wide RLHF fetch for consumer vector "
                      "to prevent B2B pain point leakage.")
    else:
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter as _FF  # noqa: PLC0415
            q = get_db().collection("leads").where(filter=_FF("tenant_id", "==", ctx.tenant_id))
            if ctx.campaign_id:
                q = q.where(filter=_FF("campaign_id", "==", ctx.campaign_id))
            else:
                log.warning("query_brain_rlhf_tenant_wide",
                            tenant_id=ctx.tenant_id[:10],
                            vector=ctx.sourcing_vector,
                            note="No campaign_id — RLHF fetch is tenant-wide. "
                                 "Pain points may span multiple vectors.")
            q = q.where(filter=_FF("status", "in", ["contacted", "converted"])).limit(20)
            docs = list(q.stream())
            ctx.pain_points = [
                d.to_dict().get("pain_point", "")
                for d in docs if d.to_dict().get("pain_point")
            ]
            ctx.has_local_history = len(ctx.pain_points) > 0
        except Exception as exc:
            log.warning("query_brain_rlhf_fetch_failed", error=str(exc))

    # ── Step 1b: Negative RLHF — Shadow Ledger rejection footprints ───────────
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter as _FF2  # noqa: PLC0415
        import concurrent.futures as _cf

        def _fetch_rejections():
            _db  = get_db()
            q_rej = _db.collection("leads").where(filter=_FF2("tenant_id", "==", ctx.tenant_id))
            if ctx.campaign_id:
                q_rej = q_rej.where(filter=_FF2("campaign_id", "==", ctx.campaign_id))
            q_rej = q_rej.where(filter=_FF2("status", "==", "rejected")).limit(30)
            docs_rej  = list(q_rej.stream())
            _domains:      list[str] = []
            _title_frags:  list[str] = []
            JUNK_TITLE_PATTERNS = [
                "jobs", "careers", "hiring", "directory", "listing",
                "aggregator", "yellow pages", "just dial",
            ]
            for d in docs_rej:
                dd = d.to_dict() or {}
                domain = (
                    dd.get("target_domain")
                    or dd.get("domain")
                    or ""
                ).strip().lower()
                if domain and domain not in ("n/a", "unknown", "") and "." in domain:
                    _domains.append(domain)
                title = (dd.get("title") or dd.get("raw_query") or "").lower()
                for pattern in JUNK_TITLE_PATTERNS:
                    if pattern in title and pattern not in _title_frags:
                        _title_frags.append(pattern)
            return _domains, _title_frags

        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
            _fut = _pool.submit(_fetch_rejections)
            ctx.neg_domains, ctx.neg_title_frags = _fut.result(timeout=2.0)

        if ctx.neg_domains or ctx.neg_title_frags:
            log.info(
                "query_brain_neg_rlhf_loaded",
                domains=len(ctx.neg_domains),
                title_frags=len(ctx.neg_title_frags),
                tenant_id=ctx.tenant_id[:10],
                campaign_id=ctx.campaign_id,
            )
    except Exception as _neg_exc:
        log.debug("query_brain_neg_rlhf_failed", error=str(_neg_exc))

    _p_cat = (ctx.persona_category or "general").strip() or "general"
    if ctx.campaign_id:
        _p_cat = f"{ctx.campaign_id}_{_p_cat}"

    # ── Step 2: Confidence threshold router ───────────────────────────────────
    _CONF_THRESHOLD = 1000.0
    try:
        cfg = get_db().collection("system_config").document("router").get().to_dict() or {}
        _CONF_THRESHOLD = float(cfg.get("intent_confidence_threshold", 1000))
    except Exception:
        pass

    _confidence  = 0.0
    _router_mode = "GEMINI_FALLBACK"

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            def _query_confidence():
                bq = get_bq_client()
                q_str = """
                    SELECT COALESCE(SUM(yield_weight), 0) AS total_confidence
                    FROM `{project}.swarm_analytics.Intent_Keywords`
                    WHERE (tenant_id = @tid OR tenant_id = 'GLOBAL')
                      AND persona_category = @cat
                """.format(project=PROJECT_ID)
                from google.cloud import bigquery as _bq  # type: ignore[import]
                job = bq.query(
                    q_str,
                    job_config=_bq.QueryJobConfig(query_parameters=[
                        _bq.ScalarQueryParameter("tid", "STRING", ctx.tenant_id),
                        _bq.ScalarQueryParameter("cat", "STRING", _p_cat),
                    ]),
                    location="asia-south1",
                )
                rows = list(job.result(timeout=3))
                return float(rows[0]["total_confidence"]) if rows else 0.0

            fut          = pool.submit(_query_confidence)
            _confidence  = fut.result(timeout=3.0)

        if _confidence >= _CONF_THRESHOLD:
            _router_mode = "STATISTICAL"

    except concurrent.futures.TimeoutError:
        log.warning("query_brain_confidence_timeout")
    except Exception as exc:
        log.warning("query_brain_confidence_failed", error=str(exc))

    log.info("query_brain_router",
             persona=_p_cat, mode=_router_mode, confidence=int(_confidence),
             tenant_id=ctx.tenant_id, campaign_id=ctx.campaign_id)

    # ── Step 3a: STATISTICAL path ──────────────────────────────────────────────
    if _router_mode == "STATISTICAL":
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                def _fetch_ngrams():
                    bq = get_bq_client()
                    ng_q = """
                        SELECT n_gram, SUM(yield_weight) AS w
                        FROM `{project}.swarm_analytics.Intent_Keywords`
                        WHERE (tenant_id = @tid OR tenant_id = 'GLOBAL')
                          AND persona_category = @cat
                        GROUP BY n_gram ORDER BY w DESC LIMIT 3
                    """.format(project=PROJECT_ID)
                    from google.cloud import bigquery as _bq  # type: ignore[import]
                    job = bq.query(
                        ng_q,
                        job_config=_bq.QueryJobConfig(query_parameters=[
                            _bq.ScalarQueryParameter("tid", "STRING", ctx.tenant_id),
                            _bq.ScalarQueryParameter("cat", "STRING", _p_cat),
                        ]),
                        location="asia-south1",
                    )
                    rows = list(job.result(timeout=3))
                    return [r["n_gram"] for r in rows if r["n_gram"]]

                top_ngrams = pool.submit(_fetch_ngrams).result(timeout=3.5)

            if top_ngrams:
                ctx.pain_points = top_ngrams[:3]
                kw_str = ", ".join(ctx.target_audience) if ctx.target_audience else ""
                for ng in top_ngrams[:3]:
                    ctx.intents.append(
                        f'"{ng}" AND ({kw_str})' if kw_str else f'"{ng}"'
                    )
                if ctx.bio and top_ngrams:
                    ctx.symptom_dorks = [
                        f'"{top_ngrams[0]}"',
                    ]
                log.info("query_brain_statistical_built",
                         query_count=len(ctx.intents), ngrams=top_ngrams)
            else:
                _router_mode = "GEMINI_FALLBACK"
                log.info("query_brain_statistical_no_ngrams_degrading")

        except Exception as exc:
            _router_mode = "GEMINI_FALLBACK"
            log.warning("query_brain_statistical_failed", error=str(exc))

    # ── Step 3b: GEMINI FALLBACK ───────────────────────────────────────────────
    if _router_mode == "GEMINI_FALLBACK":
        kw_str       = ", ".join(ctx.target_audience) if ctx.target_audience else ""
        vector_label = ctx.sourcing_vector or "B2B"
        history_ctx  = json.dumps(ctx.pain_points) if ctx.pain_points else "[]"

        # FIX (2026-06-20): Vector-aware prompt branching.
        # B2C / Real Estate / Property campaigns get a consumer-oriented prompt
        # that explicitly suppresses corporate B2B jargon ("Weak brand story",
        # "unclear positioning", etc.) which was being hallucinated by Gemini
        # because the old prompt hardcoded "B2B" in TASK 1.
        _is_consumer_vector = _is_consumer_archetype(vector_label)

        if _is_consumer_vector:
            # ── CONSUMER PROMPT (V23.6 — Unified OSINT / Anti-SEO) ─────────
            unified_prompt = f"""You are the Sideio Query Brain, operating as an elite OSINT investigator. Your goal is to find raw, hidden, unpolished web footprints of people or businesses experiencing specific pain points.

# TASK 1 — RLHF HISTORICAL MINING
Extract up to 3 short trend phrases from successful lead pain_points. Context domain: {vector_label}.
Data: {history_ctx}
CRITICAL: If Data is empty or is '[]', you MUST return an empty array [] for historical_phrases. Do NOT synthesize placeholder data.

# TASK 2 — SYMPTOM DORKING (ANTI-SEO PROTOCOL)
Target Pain Point / Bio: '{ctx.bio}'.
Generate exactly 3 Google Search operator strings (Boolean dorks) to find RAW, unfiltered web footprints of prospects experiencing this problem.
Rule: Focus purely on symptoms, complaints, and unpolished data (e.g., filetype:pdf, inurl:forum, intitle:"help with").
Rule: You MUST bypass SEO-optimized directories, aggregators, and marketing blogs.
Rule: Every single query MUST include this exact negative payload to nuke SEO spam: -site:yelp.com -site:expertise.com -site:g2.com -site:capterra.com -site:upwork.com -directory -listicle -"top 10" -"best" -shop -cart -amazon
Rule: NEVER append AND {{location}} or AND {{city}} or AND {{country}} at the end of a query. Weave the geographic context organically into the search operators (e.g., intitle:"Oman" or site:.om). The Serper API handles geo-bounding separately.

# TASK 3 — INTENT EXPANSION
Audience: '{kw_str}'. Context: '{vector_label}'.
Translate the pain point into exactly 3 natural-language conversational queries that a frustrated person or operator might ask on a niche forum, help board, or community group. Do not use generic commercial keywords.

Return ONLY the JSON object. No explanation, no markdown."""
        else:
            # ── STANDARD PROMPT (V23.6 — Unified OSINT / Anti-SEO) ────────
            unified_prompt = f"""You are the Sideio Query Brain, operating as an elite OSINT investigator. Your goal is to find raw, hidden, unpolished web footprints of people or businesses experiencing specific pain points.

# TASK 1 — RLHF HISTORICAL MINING
Extract up to 3 short trend phrases from successful lead pain_points. Context domain: {vector_label}.
Data: {history_ctx}
CRITICAL: If Data is empty or is '[]', you MUST return an empty array [] for historical_phrases. Do NOT synthesize placeholder data.

# TASK 2 — SYMPTOM DORKING (ANTI-SEO PROTOCOL)
Target Pain Point / Bio: '{ctx.bio}'.
Generate exactly 3 Google Search operator strings (Boolean dorks) to find RAW, unfiltered web footprints of prospects experiencing this problem.
Rule: Focus purely on symptoms, complaints, and unpolished data (e.g., filetype:pdf, inurl:forum, intitle:"help with").
Rule: You MUST bypass SEO-optimized directories, aggregators, and marketing blogs.
Rule: Every single query MUST include this exact negative payload to nuke SEO spam: -site:yelp.com -site:expertise.com -site:g2.com -site:capterra.com -site:upwork.com -directory -listicle -"top 10" -"best" -shop -cart -amazon
Rule: NEVER append AND {{location}} or AND {{city}} or AND {{country}} at the end of a query. Weave the geographic context organically into the search operators (e.g., intitle:"Oman" or site:.om). The Serper API handles geo-bounding separately.

# TASK 3 — INTENT EXPANSION
Audience: '{kw_str}'. Context: '{vector_label}'.
Translate the pain point into exactly 3 natural-language conversational queries that a frustrated person or operator might ask on a niche forum, help board, or community group. Do not use generic commercial keywords.

Return ONLY the JSON object. No explanation, no markdown."""

        # System instruction — OSINT / Anti-SEO compliance guard (V23.6)
        _system_instruction = (
            f"You are the Sideio Query Brain operating as an elite OSINT investigator in {vector_label} mode. "
            "Your absolute mission is to find RAW, unpolished web footprints of real intent — "
            "not SEO-optimized directories, listicles, or marketing blogs.\n\n"
            "ANTI-SEO MANDATE:\n"
            "Every symptom_dork you generate MUST actively bypass SEO spam. "
            "Use advanced Google operators (filetype:, inurl:forum, intitle:, site: for niche domains) "
            "and aggressive negative payloads (-site:yelp.com -site:g2.com -directory -listicle -\"top 10\"). "
            "Never produce queries that would return listicle pages, review aggregators, or paid directories.\n\n"
            "PERSONA ISOLATION:\n"
            "Process context strictly on a single persona vector. Do not mix or extract "
            "trend phrases across decoupled business domains.\n\n"
            "Output Format Example:\n"
            "{\n"
            "  \"historical_phrases\": [],\n"
            "  \"symptom_dorks\": [\"symptom_operator_alpha\", \"symptom_operator_beta\", \"symptom_operator_gamma\"],\n"
            "  \"translated_queries\": [\"conversational_query_alpha\", \"conversational_query_beta\", \"conversational_query_gamma\"]\n"
            "}\n\n"
            "ABSOLUTE COMPLIANCE GUARD:\n"
            "If the provided historical dataset is empty, is '[]', or contains no entries, "
            "you MUST return an empty array [] for historical_phrases. "
            "Do NOT synthesize, hallucinate, or generate placeholder data under any circumstances. "
            "Do not output sample data from this instruction block.\n\n"
            "RAW QUERY OUTPUT MANDATE:\n"
            "CRITICAL: You are a search engine query generator. "
            "YOU MUST OUTPUT ONLY THE RAW BOOLEAN DORK for each symptom_dork entry. "
            "Do NOT include conversational text, do NOT start with 'I\\'m looking for' or 'Identify' or 'Show me' or 'Find'. "
            "Output ONLY the exact string to be typed into Google. "
            "Do not wrap the entire output in quotes unless it is a single exact-match phrase. "
            "translated_queries MAY be conversational (they represent forum-style questions), "
            "but symptom_dorks must be pure Google operator strings.\n\n"
            "GEO ISOLATION RULE:\n"
            "NEVER append AND {location} or AND {city} or AND {country} at the end of any query. "
            "The Serper API receives geo-bounding parameters separately (gl, location). "
            "If you need to target a region, weave it into the search operators organically "
            "(e.g., intitle:\"Oman\" or site:.om or inurl:oman). "
            "A trailing AND {place} destroys query precision.\n"
        )
        if _is_consumer_vector:
            _system_instruction += (
                f"\nCONSUMER CONTEXT HINT ({vector_label}):\n"
                "The target audience for this campaign is end consumers / individual buyers. "
                "When generating symptom_dorks and translated_queries, lean towards pain signals "
                "found in community forums, social threads, review complaint pages, and niche "
                f"Q&A boards relevant to the {vector_label} vertical.\n"
            )

        try:
            result = call_gemini_2_5(
                unified_prompt,
                expect_json=True,
                response_schema=_QUERY_BRAIN_SCHEMA,
                system_instruction=_system_instruction,
            )
            if isinstance(result, dict):
                ctx.pain_points = [
                    p.strip() for p in result.get("historical_phrases", [])
                    if isinstance(p, str) and p.strip()
                ][:3]
                ctx.symptom_dorks = [
                    s.strip() for s in result.get("symptom_dorks", [])
                    if isinstance(s, str) and s.strip()
                ][:3]

                # ── Runtime sanitizer: strip conversational fluff from dorks (V23.6) ──
                import re as _re
                _CONVERSATIONAL_PREFIX = _re.compile(
                    r'^(?:I\'?m looking for|Show me|Find|Identify|Search for|Look for|I need|I want)\s+',
                    _re.IGNORECASE
                )
                # FIX (V23.6): Strip trailing AND {location} suffixes the LLM appends
                # despite prompt constraints. Catches: AND Oman, AND Kerala, AND "Dubai"
                _TRAILING_AND_GEO = _re.compile(
                    r'\s+AND\s+["\']?[A-Za-z][A-Za-z\s\-]+["\']?\s*$',
                    _re.IGNORECASE
                )
                _sanitized_dorks = []
                for _dork in ctx.symptom_dorks:
                    _cleaned = _CONVERSATIONAL_PREFIX.sub('', _dork).strip()
                    # Strip trailing AND {location}
                    _geo_match = _TRAILING_AND_GEO.search(_cleaned)
                    if _geo_match:
                        log.warning("symptom_dork_trailing_AND_geo_stripped",
                                    original=_cleaned[:100],
                                    stripped_suffix=_geo_match.group().strip())
                        _cleaned = _cleaned[:_geo_match.start()].strip()
                    if _cleaned != _dork:
                        log.warning("symptom_dork_sanitized",
                                    original=_dork[:80], cleaned=_cleaned[:80])
                    if _cleaned:
                        _sanitized_dorks.append(_cleaned)
                ctx.symptom_dorks = _sanitized_dorks

                ctx.intents = [
                    q.strip() for q in result.get("translated_queries", [])
                    if isinstance(q, str) and q.strip()
                ][:3]
                log.info("query_brain_gemini_ok",
                         hist=len(ctx.pain_points),
                         symp=len(ctx.symptom_dorks),
                         tq=len(ctx.intents),
                         vector=vector_label,
                         is_consumer=_is_consumer_vector,
                         tenant_id=ctx.tenant_id,
                         campaign_id=ctx.campaign_id)
        except Exception as exc:
            log.warning("query_brain_gemini_failed", error=str(exc))

    # ── Step 4: Assemble Serper query strings ──────────────────────────────────

    def _deconflict_blacklist(query_body: str, bl: str) -> str:
        """Strip -site: exclusions from *bl* that conflict with positive site:
        operators already present in *query_body*.

        Example:
            query_body = 'site:facebook.com "property in Oman"'
            bl = '-wiki -site:facebook.com -site:linkedin.com'
            → returns '-wiki -site:linkedin.com'

        This prevents self-negating dorks where a Gemini-generated symptom dork
        targets a specific site: but the global blacklist also excludes it.
        """
        import re
        # Extract all positive site: domains from the query body.
        # FIX (V23.6): Strip URL paths (/company, /jobs) and subdomain prefixes
        # (ae.linkedin.com → linkedin.com) so that site:linkedin.com/company
        # correctly deconflicts against -site:linkedin.com in the blacklist.
        def _extract_root_domain(raw: str) -> str:
            """linkedin.com/company → linkedin.com, ae.linkedin.com → linkedin.com"""
            d = raw.lower().replace("www.", "")
            d = d.split("/")[0]  # strip path
            parts = d.split(".")
            # Keep last 2 segments (or 3 for co.uk style TLDs)
            if len(parts) > 2 and len(parts[-1]) <= 3 and len(parts[-2]) <= 3:
                d = ".".join(parts[-3:])  # e.g. bbc.co.uk
            elif len(parts) > 2:
                d = ".".join(parts[-2:])  # e.g. ae.linkedin.com → linkedin.com
            return d

        _positive_sites = set(
            _extract_root_domain(m.group(1))
            for m in re.finditer(r'(?<![- ])site:([^\s]+)', query_body)
        )
        if not _positive_sites:
            return bl
        # Filter out conflicting -site: exclusions from the blacklist
        _tokens = bl.split()
        _filtered = []
        for tok in _tokens:
            if tok.startswith("-site:"):
                _excl_domain = _extract_root_domain(tok[6:])
                if _excl_domain in _positive_sites:
                    log.info("blacklist_self_negation_prevented",
                             domain=_excl_domain,
                             note="Positive site: in query conflicts with -site: in blacklist. "
                                  "Exclusion removed.")
                    continue
            _filtered.append(tok)
        return " ".join(_filtered)

    blacklist = _DEFAULT_BLACKLIST


    # ── Pre-emptive Persona exclusions: "NOT <phrase>" targeting signals (V23.5) ─
    _pre_exclusions: list[str] = []
    for _sig in (ctx.targeting_signals or []):
        _sig_clean = (_sig or "").strip()
        if _sig_clean.upper().startswith("NOT "):
            _phrase = _sig_clean[4:].strip()
            if _phrase:
                _pre_exclusions.append(f'-"{_phrase}"')

    if _pre_exclusions:
        blacklist += " " + " ".join(_pre_exclusions)
        log.info(
            "persona_neg_signals_injected",
            count=len(_pre_exclusions),
            sample=_pre_exclusions[:3],
        )

    # Negative Signal Shield injection
    try:
        shield_domains, shield_entities = fetch_neg_shield(ctx.tenant_id)
        if shield_domains:
            blacklist += " " + " ".join(f"-site:{d}" for d in shield_domains[:15] if d)
        if shield_entities:
            blacklist += " " + " ".join(f'-intitle:"{e}"' for e in shield_entities[:10] if e)
        if shield_domains or shield_entities:
            log.info("neg_shield_injected",
                     domains=len(shield_domains), entities=len(shield_entities))
    except Exception as exc:
        log.warning("neg_shield_injection_failed", error=str(exc))

    # Negative RLHF dorking
    if ctx.neg_domains:
        _excl_sites = " ".join(f"-site:{d}" for d in ctx.neg_domains[:10] if d)
        blacklist += " " + _excl_sites
        log.info("neg_rlhf_sites_injected", count=len(ctx.neg_domains[:10]))
    if ctx.neg_title_frags:
        _excl_titles = " ".join(f'-intitle:"{t}"' for t in ctx.neg_title_frags[:5] if t)
        blacklist += " " + _excl_titles
        log.info("neg_rlhf_titles_injected", count=len(ctx.neg_title_frags[:5]))

    # V24.1.1: Cap blacklist length to prevent query explosion.
    # With all sources (base + persona signals + shield domains/entities + RLHF domains/titles),
    # the blacklist can exceed 500 chars, pushing total query length past 700+ chars.
    # This causes: (a) Serper response times >10s, (b) possible 0-result returns,
    # (c) wasted credits on queries that are too constrained to match anything.
    # Cap at 350 chars — enough for base + ~15 exclusions. Trim from tail (RLHF additions
    # are appended last and least critical).
    _MAX_BLACKLIST_LEN = 350
    if len(blacklist) > _MAX_BLACKLIST_LEN:
        _original_len = len(blacklist)
        # Split into tokens, rebuild from front, stop when budget exhausted
        _bl_tokens = blacklist.split()
        _capped = []
        _running = 0
        for _t in _bl_tokens:
            if _running + len(_t) + 1 > _MAX_BLACKLIST_LEN:
                break
            _capped.append(_t)
            _running += len(_t) + 1
        blacklist = " ".join(_capped)
        log.info("blacklist_length_capped",
                 original=_original_len, capped=len(blacklist),
                 tokens_kept=len(_capped), tokens_total=len(_bl_tokens))

    if not ctx.has_local_history:
        ctx.pain_points = []

    # ── Consumer vector guard (FIX 2026-06-20) ─────────────────────────────
    # CRITICAL: Consumer/B2C campaigns must NEVER have historical_str appended.
    # Even if campaign-scoped RLHF returned pain_points from a correctly-tagged
    # B2C lead, appending AND ("...") operators to consumer search queries
    # destroys Serper recall by adding irrelevant boolean constraints.
    # Consumer queries rely purely on intents + symptom_dorks + blacklist.
    _is_consumer = _is_consumer_archetype(ctx.sourcing_vector)
    if _is_consumer:
        if ctx.pain_points:
            log.info("query_brain_consumer_pain_points_suppressed",
                     count=len(ctx.pain_points),
                     sample=ctx.pain_points[:2],
                     vector=ctx.sourcing_vector,
                     campaign_id=ctx.campaign_id,
                     note="Consumer vectors do not use historical_str. "
                          "Pain points suppressed to prevent query suffocation.")
        ctx.pain_points = []

    # ── Post-generation sanitizer (FIX 2026-06-20) ─────────────────────────
    # For consumer vectors, scrub any B2B jargon that Gemini may have leaked
    # into translated_queries despite the prompt guards. Belt-and-suspenders.
    if _is_consumer and ctx.intents:
        _B2B_JARGON = {
            "brand story", "unclear positioning", "weak brand",
            "go-to-market", "product-market fit", "market fit",
            "lead generation", "pipeline", "stakeholder alignment",
            "enterprise sales", "saas", "b2b",
        }
        _clean_intents = []
        for tq in ctx.intents:
            tq_lower = tq.lower()
            if any(jargon in tq_lower for jargon in _B2B_JARGON):
                log.warning("query_brain_b2b_jargon_scrubbed",
                            intent=tq[:80], vector=ctx.sourcing_vector,
                            campaign_id=ctx.campaign_id)
            else:
                _clean_intents.append(tq)
        if _clean_intents:
            ctx.intents = _clean_intents
        else:
            # All intents were contaminated — fall back to keyword-based queries
            log.warning("query_brain_all_intents_scrubbed",
                        vector=ctx.sourcing_vector, campaign_id=ctx.campaign_id,
                        note="Falling back to keyword-based queries.")
            ctx.intents = []

    historical_str = ""
    if ctx.pain_points:
        phrases_esc  = [f'"{p}"' for p in ctx.pain_points[:3]]
        historical_str = " AND (" + " OR ".join(phrases_esc) + ")"

    smart_queries: list[str] = []
    kw_str = ", ".join(ctx.target_audience) if ctx.target_audience else ""

    if ctx.intents:
        for tq in ctx.intents:
            _bl = _deconflict_blacklist(f'"{tq}"{historical_str}', blacklist)
            smart_queries.append(f'"{tq}"{historical_str} {_bl}')
        log.info("query_brain_assembled",
                 count=len(ctx.intents), mode=_router_mode,
                 vector=ctx.sourcing_vector or "B2B",
                 is_consumer=_is_consumer)
    elif kw_str:
        for kw in ctx.target_audience or []:
            _bl = _deconflict_blacklist(f'("{kw}"){historical_str}', blacklist)
            smart_queries.append(f'("{kw}"){historical_str} {_bl}')

    for sd in ctx.symptom_dorks:
        _bl = _deconflict_blacklist(sd, blacklist)
        smart_queries.append(f"{sd} {_bl}")

    if ctx.sourcing_vector and ctx.sourcing_vector in VECTOR_PLATFORM_MAP:
        for dork in VECTOR_PLATFORM_MAP[ctx.sourcing_vector]:
            _bl = _deconflict_blacklist(f"{dork}{historical_str}", blacklist)
            smart_queries.append(f"{dork}{historical_str} {_bl}")
        log.info("synaptic_router_dorks_appended",
                 count=len(VECTOR_PLATFORM_MAP[ctx.sourcing_vector]),
                 vector=ctx.sourcing_vector)

    return smart_queries
