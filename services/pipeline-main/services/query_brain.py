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

VECTOR_PLATFORM_MAP: dict[str, list[str]] = {
    "Social/Forum Listening": [
        "site:reddit.com",
        "site:quora.com",
        "site:facebook.com/groups",
    ],
    "Review Hijacking": [
        "site:tripadvisor.com",
        "site:trustpilot.com",
    ],
    "Maps/GMB Targeting": [
        "site:google.com/maps",
        '"near me"',
    ],
    "Classic B2B": [
        "site:linkedin.com/company",
    ],
}

_QUERY_BRAIN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "historical_phrases": {
            "type":        "ARRAY",
            "description": "Exactly 3 short B2B trend phrases from historical lead pain_points.",
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
# Main function
# ---------------------------------------------------------------------------

def generate_smart_query(
    user_keywords: list[str],
    tenant_id: str,
    bio: str,
    sourcing_vector: Optional[str] = None,
    persona_category: Optional[str] = None,
    targeting_signals: Optional[list] = None,
) -> list[str]:
    """Generate Serper query strings via statistical router or Gemini fallback.

    Args:
        user_keywords:      List of campaign keyword strings.
        tenant_id:          Tenant UID for BQ scoping.
        bio:                Effective campaign bio.
        sourcing_vector:    Campaign sourcing vector label ("Classic B2B", etc.).
        persona_category:   Persona category for BQ intent confidence query.
        targeting_signals:  List of Persona targeting signal strings.  Any
                            entry starting with "NOT " (case-insensitive) is
                            parsed into a Serper exclusion operator:
                              "NOT digital marketing agency"
                              --> -\"digital marketing agency\"
                            Positive signals (no NOT prefix) are reserved for
                            future intent-boosting (currently a no-op).

    Returns:
        List of ready-to-use Serper query strings (may be empty on error).
    """
    from services.neg_shield import fetch_neg_shield  # type: ignore[import]
    from services.gemini_service import call_gemini_2_5  # type: ignore[import]
    import json

    # ── Step 1: RLHF history (Firestore read) ─────────────────────────────────
    pain_points:      list[str] = []
    neg_domains:      list[str] = []   # domains from rejected/low-score leads
    neg_title_frags:  list[str] = []   # title patterns (job boards, directories)
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter as _FF  # noqa: PLC0415
        # BUG-QB1 FIX: Deprecated positional .where() → FieldFilter.
        # .where("status", "in", [...]) deprecated in firestore >= 2.13.
        q = (
            get_db().collection("leads")
            .where(filter=_FF("tenant_id", "==", tenant_id))
            .where(filter=_FF("status", "in", ["contacted", "converted"]))
            .limit(20)
        )
        docs = list(q.stream())
        if not docs:
            q    = get_db().collection("leads").where(filter=_FF("status", "in", ["contacted", "converted"])).limit(20)
            docs = list(q.stream())
        pain_points = [
            d.to_dict().get("pain_point", "")
            for d in docs if d.to_dict().get("pain_point")
        ]
    except Exception as exc:
        log.warning("query_brain_rlhf_fetch_failed", error=str(exc))

    # ── Step 1b: Negative RLHF — Shadow Ledger rejection footprints ───────────
    # Query rejected / low-score leads for this tenant and extract common
    # junk footprints (domain = N/A, job board titles, directory patterns).
    # These become dynamic Google Search exclusion operators: -site:X, -intitle:"Y".
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter as _FF2  # noqa: PLC0415
        import concurrent.futures as _cf

        def _fetch_rejections():
            _db  = get_db()
            # Primary: explicit rejections
            q_rej = (
                _db.collection("leads")
                .where(filter=_FF2("tenant_id", "==", tenant_id))
                .where(filter=_FF2("status", "==", "rejected"))
                .limit(30)
            )
            docs_rej  = list(q_rej.stream())
            _domains:      list[str] = []
            _title_frags:  list[str] = []
            JUNK_TITLE_PATTERNS = [
                "jobs", "careers", "hiring", "directory", "listing",
                "aggregator", "yellow pages", "just dial",
            ]
            for d in docs_rej:
                dd = d.to_dict() or {}
                # Extract domain from target_url or target_domain
                domain = (
                    dd.get("target_domain")
                    or dd.get("domain")
                    or ""
                ).strip().lower()
                score = dd.get("score", 10)
                # Skip N/A domains and already-blacklisted generics
                if domain and domain not in ("n/a", "unknown", "") and "." in domain:
                    _domains.append(domain)
                # Extract title for junk pattern detection
                title = (dd.get("title") or dd.get("raw_query") or "").lower()
                for pattern in JUNK_TITLE_PATTERNS:
                    if pattern in title and pattern not in _title_frags:
                        _title_frags.append(pattern)
            return _domains, _title_frags

        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
            _fut = _pool.submit(_fetch_rejections)
            neg_domains, neg_title_frags = _fut.result(timeout=2.0)

        if neg_domains or neg_title_frags:
            log.info(
                "query_brain_neg_rlhf_loaded",
                domains=len(neg_domains),
                title_frags=len(neg_title_frags),
                tenant_id=tenant_id[:10],
            )
    except Exception as _neg_exc:
        # Non-critical — never block the query pipeline over negative RLHF
        log.debug("query_brain_neg_rlhf_failed", error=str(_neg_exc))


    _p_cat = (persona_category or "general").strip() or "general"

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
                        _bq.ScalarQueryParameter("tid", "STRING", tenant_id),
                        _bq.ScalarQueryParameter("cat", "STRING", _p_cat),
                    ]),
                    location="asia-south1",  # REGIONALITY FIX: never rely on SDK default
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
             persona=_p_cat, mode=_router_mode, confidence=int(_confidence))

    # ── Step 3a: STATISTICAL path ──────────────────────────────────────────────
    historical_phrases: list[str] = []
    symptom_dorks:      list[str] = []
    translated_queries: list[str] = []

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
                            _bq.ScalarQueryParameter("tid", "STRING", tenant_id),
                            _bq.ScalarQueryParameter("cat", "STRING", _p_cat),
                        ]),
                        location="asia-south1",  # REGIONALITY FIX: never rely on SDK default
                    )
                    rows = list(job.result(timeout=3))
                    return [r["n_gram"] for r in rows if r["n_gram"]]

                top_ngrams = pool.submit(_fetch_ngrams).result(timeout=3.5)

            if top_ngrams:
                historical_phrases = top_ngrams[:3]
                kw_str = ", ".join(user_keywords) if user_keywords else ""
                for ng in top_ngrams[:3]:
                    translated_queries.append(
                        f'"{ng}" AND ({kw_str})' if kw_str else f'"{ng}"'
                    )
                if bio and top_ngrams:
                    symptom_dorks = [
                        f'site:linkedin.com "{top_ngrams[0]}" AND ("{bio[:40]}")',
                        f'site:reddit.com "{top_ngrams[0]}"',
                    ]
                log.info("query_brain_statistical_built",
                         query_count=len(translated_queries), ngrams=top_ngrams)
            else:
                _router_mode = "GEMINI_FALLBACK"
                log.info("query_brain_statistical_no_ngrams_degrading")

        except Exception as exc:
            _router_mode = "GEMINI_FALLBACK"
            log.warning("query_brain_statistical_failed", error=str(exc))

    # ── Step 3b: GEMINI FALLBACK ───────────────────────────────────────────────
    if _router_mode == "GEMINI_FALLBACK":
        kw_str       = ", ".join(user_keywords) if user_keywords else ""
        vector_label = sourcing_vector or "Classic B2B"
        history_ctx  = json.dumps(pain_points) if pain_points else "[]"

        unified_prompt = f"""You are the Sideio Query Brain. Perform ALL THREE tasks in a single response.

# TASK 1 — RLHF HISTORICAL MINING
Extract exactly 3 short B2B trend phrases from successful lead pain_points.
Data: {history_ctx}

# TASK 2 — SYMPTOM DORKING
User solves: '{bio}'.
Generate exactly 3 Google Search operator strings targeting prospects experiencing this problem.
Rule: ≥1 query MUST target social/professional networks.
Rule: Every query MUST include negative keywords (-shop -cart -amazon -wiki -jobs).

# TASK 3 — INTENT EXPANSION
Audience: '{kw_str}'. Vector: '{vector_label}'.
Translate into exactly 3 natural-language conversational queries for this platform.

Return ONLY the JSON object. No explanation, no markdown."""

        try:
            result = call_gemini_2_5(
                unified_prompt,
                expect_json=True,
                response_schema=_QUERY_BRAIN_SCHEMA,
            )
            if isinstance(result, dict):
                historical_phrases = [
                    p.strip() for p in result.get("historical_phrases", [])
                    if isinstance(p, str) and p.strip()
                ][:3]
                symptom_dorks = [
                    s.strip() for s in result.get("symptom_dorks", [])
                    if isinstance(s, str) and s.strip()
                ][:3]
                translated_queries = [
                    q.strip() for q in result.get("translated_queries", [])
                    if isinstance(q, str) and q.strip()
                ][:3]
                log.info("query_brain_gemini_ok",
                         hist=len(historical_phrases),
                         symp=len(symptom_dorks),
                         tq=len(translated_queries))
        except Exception as exc:
            log.warning("query_brain_gemini_failed", error=str(exc))

    # ── Step 4: Assemble Serper query strings ──────────────────────────────────
    blacklist = _DEFAULT_BLACKLIST

    # ── Pre-emptive Persona exclusions: "NOT <phrase>" targeting signals (V23.5) ─
    # These are injected FIRST — before Shadow Ledger or neg_shield — so they
    # are guaranteed to appear in every generated Serper query string.
    #
    # Parsing rule (case-insensitive):
    #   "NOT digital marketing agency"  →  -"digital marketing agency"
    #   "NOT site:linkedin.com/jobs"    →  -"site:linkedin.com/jobs"
    #
    # Positive signals (no "NOT " prefix) are a no-op here; reserved for
    # future intent-weight boosting in the statistical RLHF path.
    _pre_exclusions: list[str] = []
    for _sig in (targeting_signals or []):
        _sig_clean = (_sig or "").strip()
        if _sig_clean.upper().startswith("NOT "):
            _phrase = _sig_clean[4:].strip()   # strip the 4-char "NOT " prefix
            if _phrase:
                _pre_exclusions.append(f'-"{_phrase}"')

    if _pre_exclusions:
        blacklist += " " + " ".join(_pre_exclusions)
        log.info(
            "persona_neg_signals_injected",
            count=len(_pre_exclusions),
            sample=_pre_exclusions[:3],
        )

    # Negative Signal Shield injection (static Neo4j-style exclusion list from Shadow Ledger)
    try:
        shield_domains, shield_entities = fetch_neg_shield(tenant_id)
        if shield_domains:
            blacklist += " " + " ".join(f"-site:{d}" for d in shield_domains[:15] if d)
        if shield_entities:
            blacklist += " " + " ".join(f'-intitle:"{e}"' for e in shield_entities[:10] if e)
        if shield_domains or shield_entities:
            log.info("neg_shield_injected",
                     domains=len(shield_domains), entities=len(shield_entities))
    except Exception as exc:
        log.warning("neg_shield_injection_failed", error=str(exc))

    # Negative RLHF dorking — dynamic exclusion from rejection footprints (V23.5)
    # Appends -site: and -intitle: operators derived from leads the campaign rejected,
    # steering Serper away from historically junk sources (job boards, dirs, thin pages).
    if neg_domains:
        # Cap at 10 to avoid query string overflow (~2048 char Google limit)
        _excl_sites = " ".join(f"-site:{d}" for d in neg_domains[:10] if d)
        blacklist += " " + _excl_sites
        log.info("neg_rlhf_sites_injected", count=len(neg_domains[:10]))
    if neg_title_frags:
        _excl_titles = " ".join(f'-intitle:"{t}"' for t in neg_title_frags[:5] if t)
        blacklist += " " + _excl_titles
        log.info("neg_rlhf_titles_injected", count=len(neg_title_frags[:5]))

    historical_str = ""
    if historical_phrases:
        phrases_esc  = [f'"{p}"' for p in historical_phrases[:3]]
        historical_str = " AND (" + " OR ".join(phrases_esc) + ")"

    smart_queries: list[str] = []
    kw_str = ", ".join(user_keywords) if user_keywords else ""

    if translated_queries:
        for tq in translated_queries:
            smart_queries.append(f'"{tq}"{historical_str} {blacklist}')
        log.info("query_brain_assembled",
                 count=len(translated_queries), mode=_router_mode,
                 vector=sourcing_vector or "Classic B2B")
    elif kw_str:
        for kw in user_keywords or []:
            smart_queries.append(f'("{kw}"){historical_str} {blacklist}')

    for sd in symptom_dorks:
        smart_queries.append(f"{sd} {blacklist}")

    if sourcing_vector and sourcing_vector in VECTOR_PLATFORM_MAP:
        for dork in VECTOR_PLATFORM_MAP[sourcing_vector]:
            smart_queries.append(f"{dork}{historical_str} {blacklist}")
        log.info("synaptic_router_dorks_appended",
                 count=len(VECTOR_PLATFORM_MAP[sourcing_vector]),
                 vector=sourcing_vector)

    return smart_queries
