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
import datetime
import os
import sys
from typing import Optional

# Make this module importable both in the normal service runtime and when the
# smoke gate loads it directly from file via importlib.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE_ROOT = os.path.dirname(_HERE)
_MONOREPO_SERVICES_ROOT = os.path.dirname(_SERVICE_ROOT)
for _path in (_SERVICE_ROOT, _MONOREPO_SERVICES_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from core.logging import get_logger   # type: ignore[import]
from core.clients import get_db, get_bq_client  # type: ignore[import]
from core.config import PROJECT_ID  # type: ignore[import]
from shared.intelligence_profile import build_intelligence_strategy_plan  # type: ignore[import]

log = get_logger("pipeline.query_brain")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


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
    "-www.zoominfo.com -www.ibm.com -www.amazon.com "
    "-site:upwork.com -site:fiverr.com -site:freelancer.com -site:behance.net "
    "-\"our services\" -\"our portfolio\" -\"case studies\" -\"we offer\""
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

# V24.3 (L2-2): Import from core.constants — single source of truth at runtime.
# Falls back to inline definitions when core is not on sys.path (e.g. the
# smoke-gate loads this file via importlib in an isolated namespace).
try:
    from core.constants import (  # type: ignore[import]
        CONSUMER_ARCHETYPES as _CONSUMER_ARCHETYPES,
        D2C_ARCHETYPES      as _D2C_ARCHETYPES,
        B2B2C_ARCHETYPES    as _B2B2C_ARCHETYPES,
    )
except ImportError:
    # Fallback — kept in sync with core/constants.py manually.
    # The CI smoke gate verifies this file in isolation; the live container
    # always has core/ on PYTHONPATH so the import above succeeds.
    _CONSUMER_ARCHETYPES: frozenset = frozenset({"B2C", "B2B2C", "D2C"})
    _D2C_ARCHETYPES:      frozenset = frozenset({"D2C"})
    _B2B2C_ARCHETYPES:    frozenset = frozenset({"B2B2C"})


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
        vocabulary_notes: str = "",
        strategy: str = "COLLOQUIAL_DISCOVERY",
    ):
        self.campaign_id = campaign_id
        self.tenant_id = tenant_id
        self.target_audience = list(user_keywords) if user_keywords else []
        self.bio = bio or ""
        self.sourcing_vector = sourcing_vector
        self.persona_category = persona_category
        self.targeting_signals = list(targeting_signals) if targeting_signals else []
        # V26 Multi-Strategy OSINT Engine fields
        self.vocabulary_notes = vocabulary_notes or ""
        self.strategy = (strategy or "COLLOQUIAL_DISCOVERY").upper().strip()
        
        # Scoped dynamic arrays
        self.pain_points: list[str] = []
        self.intents: list[str] = []
        self.neg_domains: list[str] = []
        self.neg_title_frags: list[str] = []
        self.symptom_dorks: list[str] = []
        self.has_local_history: bool = False


def _fix_multi_site_query(query: str) -> str:
    """Convert broken multi-site AND queries to OR'd syntax.

    Gemini generates: site:reddit.com site:consumercomplaints.in ("term")
    Google treats space as AND → impossible → 0 results.
    Fix: (site:reddit.com OR site:consumercomplaints.in) ("term")

    Also handles negative site: operators (-site:) which must NOT be ORed.
    """
    import re as _re
    # Find all POSITIVE site: operators (not preceded by -)
    _positive_sites = _re.findall(r'(?<!-)\bsite:\S+', query)
    if len(_positive_sites) <= 1:
        return query
    # Remove all positive site: operators from query body
    _cleaned = query
    for s in _positive_sites:
        _cleaned = _cleaned.replace(s, '', 1)
    _cleaned = _cleaned.strip()
    # Rebuild with OR grouping
    _site_group = ' OR '.join(_positive_sites)
    _result = f'({_site_group}) {_cleaned}'
    # Clean up double spaces
    _result = _re.sub(r'\s{2,}', ' ', _result).strip()
    log.info("multi_site_query_fixed",
             original=query[:120], fixed=_result[:120],
             site_count=len(_positive_sites))
    return _result


def _clean_query_syntax(raw: str) -> str:
    """Optimize spacing and sanitize wildcard domain operators in queries.

    Ensures proper space separation before opening parentheses and replaces
    unsupported wildcard domains (site:*.org -> site:.org).
    """
    if not raw:
        return ""
    import re
    # 0. Fix multi-site AND → OR
    res = _fix_multi_site_query(raw)
    # 1. Strip wildcard domain prefix site:*. -> site:.
    res = re.sub(r'(?<!\w)site:\*\.', 'site:.', res)
    
    # 2. Insert missing space between quotes and opening parenthesis: "abc"(xyz) -> "abc" (xyz)
    res = re.sub(r'(?<=\")\(', ' (', res)

    # 3. Insert missing space between alphanumeric/dots/hyphens and opening parenthesis: net(xyz) -> net (xyz)
    res = re.sub(r'([a-zA-Z0-9\.\-_])\(', r'\1 (', res)
    
    # 4. Insert missing space between closing and opening parenthesis: )( -> ) (
    res = re.sub(r'\)(?=\()', ') ', res)
    
    return res


# ---------------------------------------------------------------------------
# Buyer Language Injection (V25.4.0 — Phase 2B)
# ---------------------------------------------------------------------------

_BUYER_LANGUAGE_PHRASES = (
    '"looking for"', '"need help"', '"can anyone recommend"', '"recommendations"',
)
# Pre-compiled lower-cased set for membership checks.
_BUYER_LANGUAGE_CHECK = frozenset({
    "looking for", "need help", "can anyone recommend", "recommendations",
    "recommend", "suggestion", "anyone know", "help me find",
    "where can i find", "any recommendations",
})

# Maximum total queries after buyer-language expansion.
_MAX_QUERIES_AFTER_BUYER_INJECT = 10


def _inject_buyer_language(queries: list[str]) -> list[str]:
    """Create buyer-intent variants for queries lacking buyer language.

    For each query that does not already contain buyer-language phrases,
    an additional variant is appended with buyer intent terms OR-grouped.
    The result is capped at ``_MAX_QUERIES_AFTER_BUYER_INJECT`` to respect
    downstream Serper credit budgets.

    Called after query assembly but before ``_clean_query_syntax()``.
    """
    if not queries:
        return queries

    _buyer_suffix = " (" + " OR ".join(_BUYER_LANGUAGE_PHRASES) + ")"
    result: list[str] = list(queries)  # preserve originals first

    for q in queries:
        if len(result) >= _MAX_QUERIES_AFTER_BUYER_INJECT:
            break
        q_lower = q.lower()
        if any(phrase in q_lower for phrase in _BUYER_LANGUAGE_CHECK):
            continue  # already has buyer language — skip
        result.append(q + _buyer_suffix)

    if len(result) > len(queries):
        log.info(
            "buyer_language_injected",
            original_count=len(queries),
            expanded_count=len(result),
            cap=_MAX_QUERIES_AFTER_BUYER_INJECT,
        )
    return result[:_MAX_QUERIES_AFTER_BUYER_INJECT]


# ---------------------------------------------------------------------------
# V26.0.5: Strategy 1 — Platform Mining query generator
# ---------------------------------------------------------------------------

_PLATFORM_MINING_SCHEMA = {
    "type": "object",
    "properties": {
        "platform_queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of site: inclusion queries targeting competitor platforms.",
        },
    },
    "required": ["platform_queries"],
}


def _domain_platform_targets(domain_profile: Optional[dict]) -> list[str]:
    """Extract site hosts / platform brands from a system_domain_profile."""
    if not isinstance(domain_profile, dict) or not domain_profile:
        return []
    targets: list[str] = []
    seen: set[str] = set()
    for hint in domain_profile.get("preferred_query_hints") or []:
        text = str(hint).strip()
        if not text:
            continue
        lower = text.lower()
        if "site:" in lower:
            # Pull host after site:
            for part in lower.split():
                if part.startswith("site:"):
                    host = part[5:].split("/")[0].strip()
                    if host and host not in seen:
                        seen.add(host)
                        targets.append(host)
        elif text not in seen:
            seen.add(text)
            targets.append(text)
    return targets[:8]


def _generate_platform_mining_queries(
    ctx: "CampaignQueryContext",
    bio: str,
    kw_str: str,
    vector_label: str,
    blacklist: str,
    strategy_plan: Optional[dict] = None,
    domain_profile: Optional[dict] = None,
) -> list[str]:
    """Generate site:-inclusion queries for competitor listing platforms.

    For B2C campaigns (real estate, education, health, e-commerce), the real
    leads — agents, brokers, consultants, sellers — are listed ON competitor
    platforms (Dubizzle, PropertyFinder, Savills, Practo, Sulekha, etc.).

    Pain-discovery queries miss these entirely because agent profile pages
    don't contain pain language. Platform mining queries surface the actual
    entities with contact details.

    When *domain_profile* is provided, preferred platforms / query hints seed
    both the Gemini prompt and the deterministic fallback.

    Returns:
        List of ready-to-use Serper queries with site: operators.
        Empty list on Gemini failure (non-fatal).
    """
    from services.gemini_service import call_gemini_2_5  # type: ignore[import]

    domain_family = ""
    domain_platform_lines = ""
    domain_targets = _domain_platform_targets(domain_profile)
    if isinstance(domain_profile, dict) and domain_profile:
        domain_family = str(domain_profile.get("domain_family") or "").strip()
        if domain_targets:
            domain_platform_lines = (
                "DOMAIN PROFILE PREFERRED PLATFORMS (prioritise these when valid "
                f"for the geography):\n- " + "\n- ".join(domain_targets) + "\n\n"
            )
            log.info(
                "query_brain_platform_mining_domain_seeds",
                campaign_id=ctx.campaign_id,
                domain_family=domain_family or None,
                seed_count=len(domain_targets),
                seeds=domain_targets[:5],
                note="Domain profile preferred platforms seeding platform-mining generation.",
            )

    _domain_family_line = f"DOMAIN FAMILY: {domain_family}\n" if domain_family else ""
    prompt = (
        f"You are a competitive intelligence analyst for lead generation.\n\n"
        f"TASK: Identify 3-5 competitor listing/directory/aggregator websites where\n"
        f"real individual leads (agents, brokers, consultants, sellers, practitioners)\n"
        f"for this business vertical are publicly listed with their profiles.\n\n"
        f"CAMPAIGN BIO:\n{bio[:500]}\n\n"
        f"KEYWORDS: {kw_str[:300]}\n"
        f"VERTICAL: {vector_label}\n"
        f"{_domain_family_line}"
        f"\n"
        f"{domain_platform_lines}"
        f"For each platform, generate a Google search query using site: operator\n"
        f"that would find individual profiles (with names, contact info, listings).\n\n"
        f"EXAMPLES:\n"
        f"  Real Estate: site:dubizzle.com.om agent Muscat villa\n"
        f"  Real Estate: site:propertyfinder.com agent profile Oman\n"
        f"  Real Estate: site:bayut.com broker listings\n"
        f"  Manufacturing: site:indiamart.com supplier RFQ equipment\n"
        f"  Manufacturing: site:thomasnet.com manufacturer contact\n"
        f"  Education: site:shiksha.com consultant profile\n"
        f"  Health: site:practo.com doctor clinic\n"
        f"  Services: site:sulekha.com provider profile\n"
        f"  Services: site:justdial.com business contact\n\n"
        f"RULES:\n"
        f"- Use ONLY real, known listing platforms for this vertical and geography.\n"
        f"- Prefer DOMAIN PROFILE PREFERRED PLATFORMS when listed above.\n"
        f"- Each query must have exactly ONE site: operator.\n"
        f"- Include entity terms: agent, broker, consultant, profile, contact, listings.\n"
        f"- Include geographic terms from the campaign context.\n"
        f"- Do NOT use quotes around the entire query.\n"
        f"- Do NOT include negative operators (no -site:, no -wiki, etc.).\n"
        f"- Do NOT include the client's own domain.\n"
        f"- Return ONLY the JSON object.\n"
    )

    try:
        result = call_gemini_2_5(
            prompt,
            expect_json=True,
            response_schema=_PLATFORM_MINING_SCHEMA,
        )
        if not isinstance(result, dict):
            log.warning(
                "platform_mining_bad_response",
                response_type=type(result).__name__,
                campaign_id=ctx.campaign_id,
            )
            return []

        raw_queries = [
            q.strip() for q in result.get("platform_queries", [])
            if isinstance(q, str) and q.strip() and "site:" in q.lower()
        ]

        # Validate: must contain site: operator, must be reasonable length
        validated: list[str] = []
        for q in raw_queries[:5]:
            if len(q) < 10 or len(q) > 200:
                continue
            # Don't append the full blacklist to platform mining queries —
            # these target specific platforms and the blacklist would conflict
            # with the positive site: operator. Only append minimal spam guards.
            _minimal_bl = "-wiki -jobs -careers"
            validated.append(f"{q} {_minimal_bl}")

        log.info(
            "platform_mining_queries_generated",
            campaign_id=ctx.campaign_id,
            raw_count=len(raw_queries),
            validated_count=len(validated),
        )
        if validated:
            return validated

    except Exception as exc:
        log.warning(
            "platform_mining_generation_failed",
            campaign_id=ctx.campaign_id,
            error=str(exc),
            note="Non-fatal — pain-discovery queries will still run.",
        )

    # Fallback: use strategy plan + domain profile platforms when Gemini
    # doesn't produce usable site: queries.
    platform_targets: list[str] = []
    if strategy_plan:
        platform_targets.extend(
            [p for p in strategy_plan.get("platform_targets", []) if p][:4]
        )
    # Domain preferred hosts first so vertical-correct platforms win.
    if domain_targets:
        platform_targets = list(domain_targets) + platform_targets
    # Dedupe hosts while preserving order.
    _seen_hosts: set[str] = set()
    _deduped_targets: list[str] = []
    for target in platform_targets:
        domain = str(target).strip().lower()
        if domain.startswith("http"):
            from urllib.parse import urlparse
            domain = urlparse(domain).netloc
        domain = domain.replace("site:", "").split("/")[0]
        if not domain or domain in _seen_hosts:
            continue
        _seen_hosts.add(domain)
        _deduped_targets.append(domain)
    platform_targets = _deduped_targets[:5]

    if platform_targets:
        geo_terms = []
        if strategy_plan:
            geo_terms = [g for g in strategy_plan.get("geo_terms", []) if g][:3]
        family = (domain_family or "").lower()
        fallback_queries: list[str] = []
        for domain in platform_targets:
            entity_terms = ["agent", "broker", "profile", "contact"]
            if family == "manufacturing" or "indiamart" in domain or "thomasnet" in domain:
                entity_terms = ["supplier", "manufacturer", "RFQ", "contact"]
            elif family == "real_estate" or any(
                x in domain for x in ("property", "realestate", "bayut", "dubizzle", "zillow")
            ):
                entity_terms = ["agent", "broker", "listing", "contact"]
            elif "g2" in domain or "capterra" in domain or "trustpilot" in domain:
                entity_terms = ["review", "profile", "contact"]
            base = f"site:{domain} {' '.join(entity_terms[:2])}"
            if geo_terms:
                base = f"{base} {' '.join(geo_terms)}"
            fallback_queries.append(f"{base} -wiki -jobs -careers")
        if fallback_queries:
            if domain_targets:
                log.info(
                    "query_brain_platform_mining_domain_fallback",
                    campaign_id=ctx.campaign_id,
                    domain_family=domain_family or None,
                    count=len(fallback_queries),
                    note="Built platform-mining fallback queries from domain profile seeds.",
                )
            return fallback_queries[:4]

    return []


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
    force_query_refresh: bool = False,
    vocabulary_notes: str = "",
    intelligence_strategy: Optional[dict] = None,
    campaign_name: str = "",
    location: str = "",
    pain_point: str = "",
    domain_profile: Optional[dict] = None,
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
        force_query_refresh: When True (set by produce.py exhaustion detector),
                            injects a diversification mandate into the Gemini
                            prompt to force completely new query angles.
        vocabulary_notes:   V26 — How the ICP actually speaks. When present,
                            triggers a second Gemini call to translate
                            professional queries into colloquial language.
        intelligence_strategy: V26 — Full intelligence_strategy dict from the
                            campaign document. Used for strategy-aware blacklist
                            filtering.
        campaign_name:      Campaign name for strategy-plan context.
        location:           Campaign location for geo-aware planning.
        pain_point:         Campaign pain point for strategy inference.
        domain_profile:     Optional ``system_domain_profile``. Seeds platform
                            mining with preferred sources/hints. Portfolio-level
                            block/boost still runs post-governance in produce.

    Returns:
        List of ready-to-use Serper query strings (may be empty on error).
    """
    from services.neg_shield import fetch_neg_shield  # type: ignore[import]
    from services.gemini_service import call_gemini_2_5  # type: ignore[import]
    import json

    # V26: Extract strategy from intelligence_strategy dict
    _intel_strategy = intelligence_strategy or {}
    _primary_strategy = (
        _intel_strategy.get("primary", "COLLOQUIAL_DISCOVERY")
        if isinstance(_intel_strategy, dict) else "COLLOQUIAL_DISCOVERY"
    )
    _strategy_plan = None
    if isinstance(_intel_strategy, dict) and _intel_strategy.get("primary"):
        _strategy_plan = build_intelligence_strategy_plan({
            "effective_bio": bio,
            "keywords": ", ".join(user_keywords),
            "location": location or "",
            "name": campaign_name or "",
            "pain_point": pain_point or "",
            "sourcing_vector": sourcing_vector or "",
        })
    else:
        _strategy_plan = build_intelligence_strategy_plan({
            "effective_bio": bio,
            "keywords": ", ".join(user_keywords),
            "location": location or "",
            "name": campaign_name or "",
            "pain_point": pain_point or "",
            "sourcing_vector": sourcing_vector or "",
        })
        if _strategy_plan.get("primary_strategy"):
            _primary_strategy = _strategy_plan.get("primary_strategy", _primary_strategy)

    # V26.0.4: Detect generic/thin bio and augment with vocabulary_notes
    _BIO_JUNK_PREFIXES = ("Product/Service:", "product/service:", "N/A", "n/a")
    if bio and any(bio.strip().startswith(p) for p in _BIO_JUNK_PREFIXES):
        if vocabulary_notes:
            log.info("query_brain_bio_thin_using_vocab",
                     original_bio=bio[:60],
                     vocab_preview=vocabulary_notes[:80])
            bio = f"{bio}. Buyer vocabulary: {vocabulary_notes}"

    # Instantiate the campaign scoped context to ensure absolute data boundary isolation.
    ctx = CampaignQueryContext(
        campaign_id=campaign_id,
        tenant_id=tenant_id,
        user_keywords=user_keywords,
        bio=bio,
        sourcing_vector=sourcing_vector,
        persona_category=persona_category,
        targeting_signals=targeting_signals,
        vocabulary_notes=vocabulary_notes,
        strategy=_primary_strategy,
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
            try:
                from google.cloud.firestore_v1.base_query import FieldFilter as _FF  # noqa: PLC0415
            except ImportError:
                from google.cloud.firestore_v1 import FieldFilter as _FF  # noqa: PLC0415
            q = get_db().collection("leads").where(filter=_FF("tenant_id", "==", ctx.tenant_id))
            if ctx.campaign_id:
                q = q.where(filter=_FF("campaign_id", "==", ctx.campaign_id))
            else:
                log.warning("query_brain_rlhf_tenant_wide",
                            tenant_id=ctx.tenant_id[:10],
                            vector=ctx.sourcing_vector,
                            note="No campaign_id — RLHF fetch is tenant-wide. "
                                 "Pain points may span multiple vectors.")
            # V24.5 (L8-5): Include "reviewed" leads in RLHF pool. Operators typically
            # review a lead before contacting it; excluding reviewed leads biases the
            # RLHF signal toward fast-actioned leads only.
            q = q.where(filter=_FF("status", "in", ["reviewed", "contacted", "converted"])).limit(20)
            docs = list(q.stream())
            ctx.pain_points = [
                d.to_dict().get("pain_point", "")
                for d in docs if d.to_dict().get("pain_point")
            ]
            ctx.has_local_history = len(ctx.pain_points) > 0
        except Exception as exc:
            log.warning("query_brain_rlhf_fetch_failed", error=str(exc))

    # ── Step 1b: Negative RLHF — Shadow Ledger rejection footprints ───────────
    # V24.1.1 FIX: Apply same consumer guard as positive RLHF (Step 1).
    # Without campaign_id, consumer vectors would fetch B2B rejected domains
    # and exclude valid B2C leads.
    if _skip_rlhf:
        log.info("query_brain_neg_rlhf_skipped_consumer",
                 vector=ctx.sourcing_vector, tenant_id=ctx.tenant_id[:10])
    else:
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter as _FF2  # noqa: PLC0415
            import concurrent.futures as _cf

            # V25.5.0 Phase 2D: RLHF-rejected domain TTL.
            # V26.0.4: Reduced from 30 to 7 days — 30-day TTL was too aggressive.
            # A single noise rejection blocked an entire domain for a month,
            # starving good platforms (medium.com, industry forums) of a second
            # chance. 7 days provides noise suppression without permanent starvation.
            _blacklist_ttl_days = 7
            _ttl_cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=_blacklist_ttl_days)

            def _fetch_rejections():
                _db  = get_db()
                q_rej = _db.collection("leads").where(filter=_FF2("tenant_id", "==", ctx.tenant_id))
                if ctx.campaign_id:
                    q_rej = q_rej.where(filter=_FF2("campaign_id", "==", ctx.campaign_id))
                q_rej = q_rej.where(filter=_FF2("status", "==", "rejected")).limit(30)
                docs_rej  = list(q_rej.stream())
                _domains:      list[str] = []
                _title_frags:  list[str] = []
                _ttl_expired_count = 0
                _ttl_no_ts_count = 0
                JUNK_TITLE_PATTERNS = [
                    "jobs", "careers", "hiring", "directory", "listing",
                    "aggregator", "yellow pages", "just dial",
                ]
                for d in docs_rej:
                    dd = d.to_dict() or {}

                    # V25.5.0 Phase 2D: TTL filter — skip entries rejected > 30 days ago.
                    _rejected_at = dd.get("rejected_at") or dd.get("updatedAt")
                    if _rejected_at is not None:
                        try:
                            # Firestore returns datetime objects; ensure tz-aware comparison.
                            if hasattr(_rejected_at, 'tzinfo') and _rejected_at.tzinfo is None:
                                _rejected_at = _rejected_at.replace(tzinfo=datetime.timezone.utc)
                            if _rejected_at < _ttl_cutoff:
                                _ttl_expired_count += 1
                                continue  # expired — skip this entry
                        except (TypeError, AttributeError):
                            # Non-datetime value — fail-open, keep the entry
                            _ttl_no_ts_count += 1
                    else:
                        # No timestamp field at all — fail-open, keep the entry
                        _ttl_no_ts_count += 1
                        log.debug("neg_shield_no_timestamp",
                                  doc_id=d.id,
                                  note="Rejected lead has no rejected_at/updatedAt — included (fail-open).")

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
                return _domains, _title_frags, _ttl_expired_count, _ttl_no_ts_count

            with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                _fut = _pool.submit(_fetch_rejections)
                _rej_result = _fut.result(timeout=2.0)
                ctx.neg_domains = _rej_result[0]
                ctx.neg_title_frags = _rej_result[1]
                _ttl_expired = _rej_result[2]
                _ttl_no_ts = _rej_result[3]

            # V25.5.0 Phase 2D: Log TTL filtering results.
            _total_rej_entries = len(ctx.neg_domains) + len(ctx.neg_title_frags) + _ttl_expired
            if _ttl_expired > 0 or _ttl_no_ts > 0:
                log.info(
                    "neg_shield_ttl_applied",
                    total_entries=_total_rej_entries,
                    expired_entries=_ttl_expired,
                    active_entries=len(ctx.neg_domains) + len(ctx.neg_title_frags),
                    no_timestamp_entries=_ttl_no_ts,
                    ttl_days=_blacklist_ttl_days,
                    campaign_id=ctx.campaign_id,
                )

            if ctx.neg_domains or ctx.neg_title_frags:
                log.info(
                    "query_brain_neg_rlhf_loaded",
                    domains=len(ctx.neg_domains),
                    title_frags=len(ctx.neg_title_frags),
                    tenant_id=ctx.tenant_id[:10],
                    campaign_id=ctx.campaign_id,
                )
        except Exception as _neg_exc:
            log.warning("query_brain_neg_rlhf_failed", error=str(_neg_exc))

    _p_cat = (ctx.persona_category or "general").strip() or "general"
    if ctx.campaign_id:
        _p_cat = f"{ctx.campaign_id}_{_p_cat}"

    # ── Step 2: Confidence threshold router ───────────────────────────────────
    _CONF_THRESHOLD = 100.0
    try:
        cfg = get_db().collection("system_config").document("router").get().to_dict() or {}
        _CONF_THRESHOLD = float(cfg.get("intent_confidence_threshold", 100))
    except Exception as _conf_read_err:
        # V24.3 (L1-4): Log threshold read failure — operator must know if
        # STATISTICAL routing config is inaccessible.
        log.warning("query_brain_conf_threshold_read_failed",
                    error=str(_conf_read_err),
                    fallback_threshold=_CONF_THRESHOLD,
                    note="Firestore system_config/router unreadable; "
                         "using default threshold. Check IAM permissions.")

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
                    # V24.4 (L1-2): STATISTICAL path now generates dorks for ALL top N-grams
                    # (up to 3), matching the GEMINI_FALLBACK output count. Previously only
                    # top_ngrams[0] generated a symptom_dork — the "confident" STATISTICAL
                    # path paradoxically produced fewer queries than the fallback.
                    # QRY-01 FIX: Use proper site: operators for community
                    # domains instead of unsupported bare keywords. Remove
                    # inurl: which Google deprioritizes (0-result returns).
                    _vertical_sites = "site:reddit.com OR site:quora.com OR site:community.hubspot.com"
                    ctx.symptom_dorks = [
                        f'("{ng}") ({_vertical_sites}) "complaint" OR "review"'
                        for ng in top_ngrams[:3]
                    ][:3]
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
        # V26.0.4: Augment keyword seeds with vocabulary_notes (AI-generated
        # buyer language). vocabulary_notes contains the ICP's actual search
        # terms, which are far better seeds than user-typed campaign names.
        _vocab = (ctx.vocabulary_notes or "").strip()
        if _vocab and len(kw_str) < 200:
            _vocab_terms = [t.strip() for t in _vocab.split(",") if t.strip()]
            if _vocab_terms:
                _existing = set(t.strip().lower() for t in kw_str.split(",") if t.strip())
                _new_terms = [t for t in _vocab_terms if t.lower() not in _existing]
                if _new_terms:
                    kw_str = kw_str + ", " + ", ".join(_new_terms[:8]) if kw_str else ", ".join(_new_terms[:8])
                    log.info("query_brain_vocab_seeds_injected",
                             original_kw_count=len(ctx.target_audience),
                             vocab_terms_added=len(_new_terms[:8]),
                             kw_str_preview=kw_str[:100])
        vector_label = ctx.sourcing_vector or "B2B"
        history_ctx  = json.dumps(ctx.pain_points) if ctx.pain_points else "[]"

        # FIX (2026-06-20): Vector-aware prompt branching.
        # B2C / Real Estate / Property campaigns get a consumer-oriented prompt
        # that explicitly suppresses corporate B2B jargon ("Weak brand story",
        # "unclear positioning", etc.) which was being hallucinated by Gemini
        # because the old prompt hardcoded "B2B" in TASK 1.
        _is_consumer_vector = _is_consumer_archetype(vector_label)
        # V24.3 (L2-1, L2-3): Vertical-specific prompt routing flags.
        # D2C: competitor product comparison signals.
        # B2B2C: dual-ICP — institutional buyer + individual end-user.
        _is_d2c   = (ctx.sourcing_vector or "") in _D2C_ARCHETYPES
        _is_b2b2c = (ctx.sourcing_vector or "") in _B2B2C_ARCHETYPES

        # V25.5.0 Phase 4D: Query Refresh on Exhaustion — inject diversification mandate.
        _query_refresh_instruction = ""
        if force_query_refresh:
            _query_refresh_instruction = (
                "\n\nIMPORTANT: Previous queries have been exhausted and returned zero new results "
                "for 3+ cycles. Generate COMPLETELY NEW query angles. Do NOT repeat previous patterns. "
                "Try different keyword combinations, different platforms, different buyer personas.\n"
            )
            log.info("query_brain_forced_refresh",
                     campaign_id=ctx.campaign_id,
                     note="Exhaustion detector triggered — injecting diversification mandate.")

        if _is_consumer_vector:
            # ── CONSUMER PROMPT (V24.1.1 — Differentiated from Standard) ───
            unified_prompt = f"""You are the Sideio Query Brain, operating as an elite OSINT investigator for CONSUMER ({vector_label}) campaigns. Your goal is to find raw, unpolished web footprints of individual consumers or local businesses experiencing specific pain points.

# TASK 1 — RLHF HISTORICAL MINING
Extract up to 3 short trend phrases from successful lead pain_points. Context domain: {vector_label}.
Data: {history_ctx}
CRITICAL: If Data is empty or is '[]', you MUST return an empty array [] for historical_phrases. Do NOT synthesize placeholder data.

# TASK 2 — CONSUMER SYMPTOM DORKING (SIMPLIFIED V25.3.0)
Target Pain Point / Bio: '{ctx.bio}'.
Generate exactly 3 Google Search queries to find RAW consumer complaints, reviews, and community discussions about this problem.
Rule: MAX 2 boolean groups per query. Overly complex multi-group dorks return 0 results on Google. Keep it simple.
Rule: The ONLY structural operators allowed are site: targeting (e.g., site:reddit.com, site:quora.com, site:mouthshut.com, site:consumercomplaints.in). Do NOT use inurl:, intitle:, or filetype: — Google deprioritizes and often ignores these.
Rule: Use natural buyer phrases in quotes (e.g., "terrible service" OR "worst experience") combined with ONE site: operator at most.
Rule: NEVER use B2B jargon: "lead generation", "pipeline", "go-to-market", "product-market fit", "enterprise sales", "SaaS", "B2B", "stakeholder alignment", "brand story", "unclear positioning".
REDDIT RULE: When targeting Reddit, use subreddit-specific searches.
Instead of site:reddit.com, use site:reddit.com/r/{{relevant_subreddit}}.
Suggest 2-3 subreddits likely to contain buyer discussions for this business.
NEVER target r/AskReddit, r/politics, r/news, r/worldnews, r/videos.
Rule: Add this exact negative payload to nuke SEO spam: -site:expertise.com -directory -listicle -"top 10" -"best" -shop -cart -amazon
Rule: NEVER append AND {{location}} or AND {{city}} or AND {{country}} at the end. Weave geography into natural phrases if needed.
Rule: You MUST enclose all OR clauses in parentheses. Always separate operators, keywords, quotes, and parentheses with a space.
Rule: NEVER use wildcard characters in site: (e.g. NEVER site:*.org). Use site:.org instead.

# TASK 3 — CONSUMER INTENT EXPANSION (SIMPLIFIED V25.3.0)
Audience: '{kw_str}'. Context: '{vector_label}'.
Generate exactly 3 short natural-language queries (MAX 12 WORDS each) that a frustrated individual consumer would actually type into Google or post in a community group.
Rule: Write like a real person venting, not a researcher. Examples: "worst [product] customer service experience", "[brand] keeps ignoring my complaint", "anyone else having [specific problem]".
Rule: No jargon. No corporate language. No "How do I" question starters.

Return ONLY the JSON object. No explanation, no markdown.{_query_refresh_instruction}"""
        else:
            # ── STANDARD PROMPT (V24.1.1 — B2B Buyer Friction Focus) ────────
            unified_prompt = f"""You are the Sideio Query Brain, operating as an elite OSINT investigator. Your goal is to find raw, hidden, unpolished web footprints of people or businesses experiencing specific pain points.

# TASK 1 — RLHF HISTORICAL MINING
Extract up to 3 short trend phrases from successful lead pain_points. Context domain: {vector_label}.
Data: {history_ctx}
CRITICAL: If Data is empty or is '[]', you MUST return an empty array [] for historical_phrases. Do NOT synthesize placeholder data.

# TASK 2 — SYMPTOM DORKING (SIMPLIFIED V25.3.0)
Target Pain Point / Bio: '{ctx.bio}'.
Generate exactly 3 Google Search queries to find RAW, unfiltered web footprints of prospects experiencing this problem.
Rule: MAX 2 boolean groups per query. Overly complex multi-group dorks with 4-5 boolean groups return 0 results on Google. Keep it simple.
Rule: The ONLY structural operators allowed are site: targeting (e.g., site:reddit.com, site:quora.com, site:community.hubspot.com). Do NOT use inurl:, intitle:, or filetype: — Google deprioritizes and often ignores these operators, causing 0-result returns.
Rule: PROHIBITED: site:.edu, site:.org — these return academic and non-profit content, not B2B buyers.
Rule: Use natural buyer-pain phrases in quotes (e.g., "alternative to [competitor]" OR "pricing too high") combined with ONE site: operator at most.
Rule: Focus on buyer pain symptoms and competitor friction (e.g., "alternative to", "pricing too high", "support issues", "bounce rates", "going to spam"). Do NOT use generic category keywords like "lead generation services" that match competitor websites.
REDDIT RULE: When targeting Reddit, use subreddit-specific searches.
Instead of site:reddit.com, use site:reddit.com/r/{{relevant_subreddit}}.
Suggest 2-3 subreddits likely to contain buyer discussions for this business.
NEVER target r/AskReddit, r/politics, r/news, r/worldnews, r/videos.
Rule: Add this exact negative payload to every query: -site:expertise.com -directory -listicle -"top 10" -"best" -shop -cart -amazon
Rule: NEVER append AND {{location}} or AND {{city}} at the end. Geo is handled by query text phrasing and downstream scoring.
Rule: You MUST enclose all OR clauses in parentheses. Always separate operators, keywords, quotes, and parentheses with a space.
Rule: NEVER use wildcards in site: (e.g., NEVER site:*.org). Use site:.org instead.

# TASK 3 — INTENT EXPANSION (SIMPLIFIED V25.3.0)
Audience: '{kw_str}'. Context: '{vector_label}'.
Generate exactly 3 short natural-language queries (MAX 12 WORDS each) that a frustrated business owner or practitioner would actually type into Google or Reddit.
Rule: Write like a real person venting, not a marketing blog. Examples: "[tool] attribution completely wrong anyone else?", "tried 3 email tools all going to spam", "why does [specific pain] keep getting worse".
Rule: BANNED openings — these match SEO articles, not buyers: "How do B2B companies", "How do I", "What is the best way", "What are the biggest challenges", "How can we", "Tips for".
Rule: No jargon. No corporate language. Think: what would a frustrated person with this problem actually type?

Return ONLY the JSON object. No explanation, no markdown.{_query_refresh_instruction}"""

        # System instruction — OSINT / Anti-SEO compliance guard (V23.6)
        _system_instruction = (
            f"You are the Sideio Query Brain operating as an elite OSINT investigator in {vector_label} mode. "
            "Your absolute mission is to find RAW, unpolished web footprints of real intent — "
            "not SEO-optimized directories, listicles, or marketing blogs.\n\n"
            "ANTI-SEO MANDATE (V25.3.0):\n"
            "Every symptom_dork you generate MUST actively bypass SEO spam. "
            "Use MAX 2 boolean groups per query. The ONLY structural operator allowed is site: "
            "targeting niche community domains (e.g., site:reddit.com, site:quora.com). "
            "Do NOT use inurl:, intitle:, or filetype: — Google deprioritizes these, causing 0-result returns. "
            "Use aggressive negative payloads (-directory -listicle -\"top 10\"). "
            "Never produce queries that would return listicle pages or paid directories. "
            "Review platforms (yelp, g2, capterra, trustpilot) are ALLOWED — they contain leads.\n\n"
            "BUYER INTENT VS SELLER OFFERINGS:\n"
            "You are looking for buyer paint points and competitor complaints. "
            "Do NOT generate search queries that match pages offering or advertising lead generation/outreach services. "
            "For example, search for problems (e.g., 'emails going to spam', 'Apollo alternative') "
            "rather than solutions (e.g., 'best lead generation tool', 'lead generation services').\n\n"
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
            "If you need to target a region, weave it into natural query phrases "
            "(e.g., '\"Oman\" \"pricing too high\"' or site:.om). "
            "A trailing AND {place} destroys query precision.\n\n"
            "BOOLEAN PRECEDENCE & SPACING MANDATE:\n"
            "1. You MUST enclose all OR clauses in parentheses to enforce proper operator precedence. "
            "Google treats space as implicit AND which has higher precedence than OR. "
            "Without parentheses, boolean scope leaks and dilutes results. "
            "Example: '(\"pricing too high\" OR \"too expensive\") site:reddit.com'.\n"
            "2. Never use wildcards in site: operators (e.g. NEVER output site:*.org or site:*.com). "
            "Google does not support wildcard subdomains. Use site:.org or site:.com instead.\n"
            "3. Enforce strict spacing around quotes, operators, and parentheses. "
            "For example, write 'site:boards.net (\"difficulty\")' instead of 'site:boards.net(\"difficulty\")'.\n"
            "\nPROHIBITED OPERATORS (V25.3.0):\n"
            "NEVER use any of the following in symptom_dorks — Google deprioritizes or "
            "ignores these operators, causing queries to return 0 results:\n"
            "- inurl: — any form (inurl:forum, inurl:review, inurl:complaint, etc.)\n"
            "- intitle: — any form\n"
            "- filetype: — any form (filetype:pdf, filetype:pptx, etc.)\n"
            "- site:.edu — academic content, not buyers\n"
            "- site:.org — NGOs and non-profits, not buyers\n"
            "ONLY ALLOWED structural operator: site: targeting specific community domains "
            "(e.g., site:reddit.com, site:quora.com, site:community.hubspot.com).\n\n"
            "REDDIT SUBREDDIT TARGETING (V25.4.0):\n"
            "When targeting Reddit, ALWAYS use subreddit-specific site: operators "
            "instead of the broad site:reddit.com. Choose 2-3 subreddits most likely "
            "to contain buyer discussions for this campaign's vertical.\n"
            "NEVER target generic subreddits: r/AskReddit, r/politics, r/news, "
            "r/worldnews, r/videos — these contain zero buyer intent.\n"
        )
        if _is_consumer_vector:
            _system_instruction += (
                f"\nCONSUMER CONTEXT HINT ({vector_label}):\n"
                "The target audience for this campaign is end consumers / individual buyers. "
                "When generating symptom_dorks and translated_queries, lean towards pain signals "
                "found in community forums, social threads, review complaint pages, and niche "
                f"Q&A boards relevant to the {vector_label} vertical.\n\n"
                "CONSUMER SUBREDDIT OVERRIDE (V26.0.4.3):\n"
                "For consumer campaigns, choose subreddits relevant to the consumer vertical. "
                "Examples by vertical:\n"
                "  Real Estate: site:reddit.com/r/RealEstate, site:reddit.com/r/FirstTimeHomeBuyer, "
                "site:reddit.com/r/expats\n"
                "  Education: site:reddit.com/r/ApplyingToCollege, site:reddit.com/r/studyAbroad, "
                "site:reddit.com/r/NEET\n"
                "  Health/Medical: site:reddit.com/r/HealthInsurance, site:reddit.com/r/AskDocs\n"
                "  General consumer: site:reddit.com/r/personalfinance, site:reddit.com/r/Frugal\n"
                "NEVER use B2B SaaS subreddits for consumer campaigns: "
                "r/sales, r/startups, r/SaaS, r/marketing, r/sysadmin — these are for "
                "enterprise software buyers, not end consumers.\n\n"
                "CONSUMER QUERY SIMPLICITY OVERRIDE (V25.3.0):\n"
                "Use ONLY site: operators targeting consumer review platforms "
                "(e.g., site:reddit.com, site:mouthshut.com, site:consumercomplaints.in). "
                "Do NOT use inurl: or filetype: operators — they cause 0-result returns.\n"
                "NEVER use B2B review sites for consumer campaigns: "
                "site:g2.com, site:capterra.com — these review software, not consumer products.\n"
                "For consumer reviews, use: site:yelp.com, site:trustpilot.com, "
                "site:mouthshut.com, site:consumercomplaints.in\n\n"
                "CONSUMER VOCABULARY MANDATE (V26.0.4.3):\n"
                "NEVER use these B2B/SaaS terms in ANY consumer query:\n"
                "- 'looking for tool', 'software suggestion', 'what do you use'\n"
                "- 'how do you', 'our stack', 'tech stack', 'SaaS alternative'\n"
                "- 'vendor selection', 'procurement', 'ROI'\n"
                "Instead use consumer language: 'looking for', 'anyone tried', "
                "'recommendations', 'reviews', 'worth it', 'any good'\n\n"
                "DIALOG-CUE DORKING MANDATE:\n"
                "For consumer campaigns, weave conversational dialog cues directly into queries. "
                "Include transactional or discussion reply signatures like "
                "(\"pm me\" OR \"pm sent\" OR \"still available\" OR \"send details\" OR \"anyone know\"). "
                "This forces Google to prioritize active discussion threads over marketing pages. "
                "Example dork: site:reddit.com Muscat \"villa\" (\"pm me\" OR \"still available\")\n"
            )
        else:
            # B2B campaigns get B2B-specific subreddit examples
            _system_instruction += (
                "B2B SUBREDDIT EXAMPLES:\n"
                "For B2B campaigns, target professional subreddits:\n"
                "  site:reddit.com/r/sysadmin, site:reddit.com/r/smallbusiness, "
                "site:reddit.com/r/sales, site:reddit.com/r/startups\n"
            )
        # V24.3 (L2-1): D2C competitor-comparison system instruction override.
        # D2C brands need consumer-vs-competitor signals, not local buyer pain.
        if _is_d2c:
            _system_instruction += (
                "\nD2C PRODUCT COMPARISON MANDATE:\n"
                "This is a Direct-to-Consumer brand campaign. Generate queries that find consumers "
                "who are actively comparing products or switching from competitor brands. Prioritize: "
                "'tried X alternative', 'switched from [brand]', 'honest review vs', "
                "'is [product] worth it', 'looking for alternative to'. "
                "Use site:reddit.com, site:trustpilot.com for community signals. "
                "Do NOT generate B2B SaaS or enterprise buyer signals.\n"
            )
        # V24.3 (L2-3): B2B2C dual-ICP system instruction.
        # 50% institutional + 50% end-user signals.
        if _is_b2b2c:
            _system_instruction += (
                "\nB2B2C DUAL-ICP MANDATE:\n"
                "This business sells to institutions (B2B buyer) and serves individual end-users (B2C user). "
                "Generate a MIXED query set: 50% institutional purchase signals "
                "(procurement, budget approval, vendor selection) AND 50% end-user pain signals "
                "(user complaints, review discussions, community posts). "
                "Label each query type clearly in your output.\n"
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
                    # V24.5.8: Prohibited-operator gate (code-level belt-and-suspenders).
                    # Despite the prompt instruction, Gemini still generates site:.org and
                    # site:.edu as POSITIVE target operators, returning academic/nonprofit
                    # pages instead of buyers (e.g. (site:.org OR site:.edu) "brand narrative").
                    # Strip the prohibited site: clause from the dork. If nothing remains,
                    # drop the dork entirely.
                    _PROHIBITED_POSITIVE_SITE = _re.compile(
                        r'site:\.(?:org|edu|ac\.\w+|gov|mil)\b',
                        _re.IGNORECASE
                    )
                    if _PROHIBITED_POSITIVE_SITE.search(_cleaned):
                        _before = _cleaned
                        # Strip the offending site: clauses + any surrounding OR/AND
                        _cleaned = _PROHIBITED_POSITIVE_SITE.sub('', _cleaned)
                        # Clean up orphaned boolean operators and empty parens
                        _cleaned = _re.sub(r'\(\s*(?:OR\s*)+\)', '', _cleaned)
                        _cleaned = _re.sub(r'\bOR\s+OR\b', 'OR', _cleaned)
                        _cleaned = _re.sub(r'^\s*(?:OR|AND)\s+', '', _cleaned)
                        _cleaned = _re.sub(r'\s+(?:OR|AND)\s*$', '', _cleaned)
                        _cleaned = _cleaned.strip()
                        log.warning(
                            "symptom_dork_prohibited_site_stripped",
                            original=_before[:120],
                            cleaned=_cleaned[:120],
                            note="site:.org / site:.edu as positive operator stripped — returns academic pages, not buyers",
                        )
                    if _cleaned:
                        _sanitized_dorks.append(_clean_query_syntax(_cleaned))
                ctx.symptom_dorks = _sanitized_dorks

                ctx.intents = [
                    _clean_query_syntax(q.strip()) for q in result.get("translated_queries", [])
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

                # ── V26.0.3 HYBRID: Universal Colloquial Translation ────────
                # Every strategy benefits from buyer-language queries. When
                # vocabulary_notes exist, use them. Otherwise, derive the
                # audience context from the campaign bio so no campaign is
                # left without colloquial search terms.
                _vocab_context = (ctx.vocabulary_notes or "").strip()
                if not _vocab_context and ctx.bio:
                    # Derive a lightweight audience description from the bio
                    _vocab_context = (
                        f"People who need: {ctx.bio[:300]}. "
                        "These are everyday people — not industry professionals."
                    )
                if _vocab_context:
                    _all_queries_for_translation = ctx.symptom_dorks + ctx.intents
                    if _all_queries_for_translation:
                        _colloquial_prompt = (
                            "You are a search query optimizer. "
                            "You are given professional/jargon search queries and must rewrite them "
                            "as SHORT KEYWORD PHRASES that this audience would type into Google:\n\n"
                            f"Audience: {_vocab_context}\n\n"
                            "CRITICAL RULES:\n"
                            "1. Output 3-6 word KEYWORD PHRASES, NOT full sentences.\n"
                            "2. NEVER output questions (no 'why', 'how', 'what', 'is it', '?').\n"
                            "3. NEVER output full sentences with subjects and verbs.\n"
                            "4. Use the exact slang/colloquial words this audience uses.\n"
                            "5. Remove ALL industry jargon. Use everyday language.\n\n"
                            "GOOD examples: 'cheap property Muscat', 'NEET low score MBBS abroad', "
                            "'Google Ads too expensive small business'\n"
                            "BAD examples: 'Why are property agents in Oman so unreliable?', "
                            "'Finding verified property in Muscat is impossible.'\n\n"
                            "Queries to translate:\n"
                        )
                        for _idx, _q in enumerate(_all_queries_for_translation, 1):
                            _colloquial_prompt += f"{_idx}. {_q}\n"
                        _colloquial_prompt += (
                            "\nReturn the same number of queries as SHORT keyword phrases. "
                            "Return ONLY a JSON array of strings. No explanation."
                        )

                        _COLLOQUIAL_SCHEMA = {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                        }

                        try:
                            _colloquial_result = call_gemini_2_5(
                                _colloquial_prompt,
                                expect_json=True,
                                response_schema=_COLLOQUIAL_SCHEMA,
                            )
                            if isinstance(_colloquial_result, list) and _colloquial_result:
                                _colloquial_queries = [
                                    _clean_query_syntax(q.strip())
                                    for q in _colloquial_result
                                    if isinstance(q, str) and q.strip()
                                ]
                                if _colloquial_queries:
                                    # Append colloquial variants — do NOT replace originals.
                                    # Original professional queries are kept for precision;
                                    # colloquial variants add recall from everyday searches.
                                    _orig_dork_count = len(ctx.symptom_dorks)
                                    _orig_intent_count = len(ctx.intents)
                                    # Split colloquial results back into dorks vs intents
                                    _colloquial_dorks = _colloquial_queries[:_orig_dork_count]
                                    _colloquial_intents = _colloquial_queries[_orig_dork_count:]
                                    ctx.symptom_dorks.extend(_colloquial_dorks)
                                    ctx.intents.extend(_colloquial_intents)
                                    log.info(
                                        "query_brain_colloquial_translation_ok",
                                        original_count=len(_all_queries_for_translation),
                                        colloquial_count=len(_colloquial_queries),
                                        vocabulary_notes_preview=ctx.vocabulary_notes[:80],
                                        campaign_id=ctx.campaign_id,
                                    )
                                else:
                                    log.warning(
                                        "query_brain_colloquial_empty_result",
                                        campaign_id=ctx.campaign_id,
                                        note="Gemini returned empty colloquial translations.",
                                    )
                            else:
                                log.warning(
                                    "query_brain_colloquial_unexpected_type",
                                    result_type=str(type(_colloquial_result)),
                                    campaign_id=ctx.campaign_id,
                                )
                        except Exception as _colloquial_exc:
                            # Non-fatal — colloquial translation is additive.
                            # Original queries still work without it.
                            log.warning(
                                "query_brain_colloquial_translation_failed",
                                error=str(_colloquial_exc),
                                exc_type=type(_colloquial_exc).__name__,
                                campaign_id=ctx.campaign_id,
                                note="Colloquial translation Gemini call failed. "
                                     "Continuing with professional queries only.",
                            )

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

    # ── Pre-emptive Persona exclusions: "NOT <phrase>" targeting signals (V23.5) ─
    _pre_exclusions: list[str] = []
    for _sig in (ctx.targeting_signals or []):
        _sig_clean = (_sig or "").strip()
        if _sig_clean.upper().startswith("NOT "):
            _phrase = _sig_clean[4:].strip()
            if _phrase:
                _pre_exclusions.append(f'-"{_phrase}"')
    if _pre_exclusions:
        log.info(
            "persona_neg_signals_injected",
            count=len(_pre_exclusions),
            sample=_pre_exclusions[:3],
        )

    # V24.5 (L1-5): Blacklist priority rebuild — most specific (RLHF-learned) first,
    # static defaults last. The domain count cap trims from the tail, so trimming
    # removes static defaults before removing RLHF-learned campaign-specific signals.
    _rlhf_blacklist = ""  # RLHF-learned exclusions (highest priority)
    if ctx.neg_domains:
        _rlhf_blacklist += " " + " ".join(f"-site:{d}" for d in ctx.neg_domains[:10] if d)
        log.info("neg_rlhf_sites_injected", count=len(ctx.neg_domains[:10]))
    if ctx.neg_title_frags:
        # V26.0: Length guard — skip entries > 80 chars (outreach templates, not real page titles)
        _valid_title_frags = [t for t in ctx.neg_title_frags[:5] if t and len(t) <= 80]
        _skipped_title_frags = len(ctx.neg_title_frags[:5]) - len(_valid_title_frags)
        if _skipped_title_frags > 0:
            log.warning("neg_rlhf_titles_oversized_skipped", skipped=_skipped_title_frags)
        if _valid_title_frags:
            # V26.0.3: DROPPED -intitle: operators entirely.
            # Google deprioritizes and often ignores intitle: operators, causing
            # 0-result returns. They consumed ~40% of query budget for zero
            # filtering benefit. Entity names are now excluded only via RLHF
            # feedback (domain-level -site: exclusions that actually work).
            pass

    _shield_blacklist = ""  # Neg shield domains (tenant-level)
    try:
        # V24.3 (L8-1): Pass sourcing_vector so B2B rejections do not pollute B2C
        # search quality. The neg shield BQ query now filters by vector.
        shield_domains, shield_entities = fetch_neg_shield(
            ctx.tenant_id,
            sourcing_vector=ctx.sourcing_vector or "B2B",
        )
        if shield_domains:
            _shield_blacklist += " " + " ".join(f"-site:{d}" for d in shield_domains[:15] if d)
        if shield_entities:
            # V26.0: Length guard — skip entities > 80 chars (prevents outreach
            # message templates from being injected as -intitle: exclusions)
            _valid_entities = [e for e in shield_entities[:10] if e and len(e) <= 80]
            _skipped_entities = len(shield_entities[:10]) - len(_valid_entities)
            if _skipped_entities > 0:
                log.warning("neg_shield_entities_oversized_skipped", skipped=_skipped_entities)
            if _valid_entities:
                # V26.0.3: DROPPED -intitle: operators — Google ignores them
                # and they consumed ~40% of query budget. Entity exclusions are
                # handled at the domain level (-site:) or filtered downstream.
                pass
        if shield_domains or shield_entities:
            log.info("neg_shield_injected",
                     domains=len(shield_domains), entities=len(shield_entities))
    except Exception as exc:
        log.warning("neg_shield_injection_failed", error=str(exc))
        shield_domains, shield_entities = [], []

    _persona_blacklist = ""  # Persona signals (campaign-level)
    if _pre_exclusions:
        _persona_blacklist = " " + " ".join(_pre_exclusions)

    # ── V26.0.3 HYBRID: Universal blacklist — review sites always allowed ───
    # Review/aggregator platforms contain leads for ALL strategies:
    # - PLATFORM_MINING: entities listed on the platform
    # - COMPETITOR_TOUCHPOINT: reviewers are leads
    # - COLLOQUIAL_DISCOVERY: reviewers describe pain in plain language
    # - PROFESSIONAL_NETWORK: B2B review sites (G2) have decision-maker names
    # - EVENT_TRIGGER_MINING: news sources should never be blacklisted
    #
    # The default blacklist only filters true noise (freelancer spam, portfolios).
    # Review sites (g2, capterra, yelp, trustpilot) are NOT in _DEFAULT_BLACKLIST
    # by design — they're intelligence sources, not noise.
    _effective_default_blacklist = _DEFAULT_BLACKLIST
    _strategy_upper = ctx.strategy.upper()

    # For EVENT_TRIGGER_MINING, even the default noise filters are too aggressive
    # (event-related pages sometimes contain "our portfolio" or "case studies")
    if _strategy_upper == "EVENT_TRIGGER_MINING":
        _effective_default_blacklist = ""
        log.info("blacklist_strategy_override",
                 strategy=ctx.strategy,
                 note="EVENT_TRIGGER_MINING: Minimal blacklist — only RLHF/shield exclusions active.")

    # Clean up any double spaces from replacement
    import re as _bl_clean_re
    _effective_default_blacklist = _bl_clean_re.sub(r'\s{2,}', ' ', _effective_default_blacklist).strip()

    # Build: RLHF (most specific) → shield → persona → static (least specific)
    blacklist = (_rlhf_blacklist + _shield_blacklist + _persona_blacklist + " " + _effective_default_blacklist).strip()

    # V26.0.3: Reduced from 15 to 8. Google has a ~32-word operator limit.
    # With 15 -site: domains + 10 -intitle: + base exclusions, most queries
    # exceeded the limit and Google silently ignored the overflow.
    _MAX_BLACKLIST_DOMAINS = 8
    import re as _bl_re
    # Split on operator boundaries: each token is a complete exclusion unit
    # e.g. -site:foo.com | -intitle:"long phrase here" | -"exact match" | -word
    _bl_tokens = _bl_re.findall(r'-(?:site:\S+|intitle:"[^"]*"|"[^"]*"|\S+)', blacklist)
    _site_tokens = []
    _non_site_tokens = []
    for _t in _bl_tokens:
        if _t.startswith("-site:"):
            _site_tokens.append(_t)
        elif _t.startswith("-intitle:"):
            # V26.0.3: Drop -intitle: operators entirely — Google ignores them
            # and they consume ~40% of query character budget for zero benefit.
            continue
        else:
            _non_site_tokens.append(_t)
    if len(_site_tokens) > _MAX_BLACKLIST_DOMAINS:
        _original_site_count = len(_site_tokens)
        _site_tokens = _site_tokens[:_MAX_BLACKLIST_DOMAINS]
        log.info("blacklist_length_capped",
                 original_domains=_original_site_count,
                 capped_domains=len(_site_tokens),
                 non_site_kept=len(_non_site_tokens),
                 max_domains=_MAX_BLACKLIST_DOMAINS)
    blacklist = " ".join(_site_tokens + _non_site_tokens)


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

    # ── Post-generation FAQ-opener sanitizer (V24.5.7) ─────────────────────
    # For B2B vectors: drop translated_queries that start with FAQ-phrase openers.
    # Despite the ANTI-FAQ MANDATE in TASK 3, Gemini occasionally generates
    # "How do you...", "What are good alternatives...", "What is the best way to..."
    # These match every SEO/marketing agency blog post and return zero buyers.
    # The Gemini pre-filter then classifies the results as Low confidence, so
    # these queries burn 1 credit for 0 leads. Drop them before Serper is called.
    # V26.0.4.5: Extended to ALL campaign types. Consumer campaigns also
    # generate sentences ("Confused by Oman property investment regulations.",
    # "Why is Oman real estate so hard to trust?") that waste credits.
    if ctx.intents:
        import re as _re_faq
        _FAQ_OPENERS = (
            "how do you ", "how do we ", "how do i ", "how to ",
            "what are good ", "what are the best ", "what is the best ",
            "what are common ", "what is the difference ", "what are some ",
            "tips for ", "how can we ", "how can i ", "how can you ",
            "why do ", "why does ", "why is ",
            "best practices ", "guide to ", "introduction to ",
            # V26.0.4.5: Additional sentence patterns for consumer campaigns
            "confused by ", "frustrated with ", "tired of ",
            "is it worth ", "is there a ", "is there any ",
            "anyone else ", "does anyone know ",
        )
        # V26.0.4.5: Detect sentence patterns — ends with ?, period after 4+ words,
        # or contains comma-separated clauses. These are full sentences, not keyword
        # phrases, and return 0 results when exact-match quoted.
        _SENTENCE_PATTERN = _re_faq.compile(
            r'(?:'
            r'\?$'                         # ends with question mark
            r'|,\s+\w+\??$'               # trailing clause with comma ("problem, anyone?")
            r'|^[A-Z][a-z]+\s+(?:is|are|was|were|do|does|did|can|could|should|would|will)\s'  # Sentence starts with "Subject verb..."
            r')',
        )
        _clean_intents = []
        for tq in ctx.intents:
            tq_lower = tq.lower().lstrip('"\'')
            _is_faq = any(tq_lower.startswith(opener) for opener in _FAQ_OPENERS)
            _is_sentence = bool(_SENTENCE_PATTERN.search(tq))
            if _is_faq or _is_sentence:
                log.warning(
                    "query_brain_faq_sentence_scrubbed",
                    intent=tq[:100],
                    campaign_id=ctx.campaign_id,
                    is_consumer=_is_consumer,
                    reason="faq_opener" if _is_faq else "sentence_pattern",
                    note="FAQ/sentence detected post-generation. Dropping to avoid "
                         "SEO-article results and exact-match quote failures.",
                )
            else:
                _clean_intents.append(tq)
        if _clean_intents:
            ctx.intents = _clean_intents
        elif ctx.intents:
            # All intents were FAQ/sentences — keep one as last resort
            log.warning(
                "query_brain_all_intents_faq",
                campaign_id=ctx.campaign_id,
                note="ALL translated_queries were FAQ/sentences. Keeping 1 as last resort.",
            )
            ctx.intents = ctx.intents[:1]

    historical_str = ""
    if ctx.pain_points:
        phrases_esc  = [f'"{p}"' for p in ctx.pain_points[:3]]
        historical_str = " (" + " OR ".join(phrases_esc) + ")"

    smart_queries: list[str] = []
    kw_str = ", ".join(ctx.target_audience) if ctx.target_audience else ""

    if ctx.intents:
        for tq in ctx.intents:
            # V24.1.4 FIX: Skip empty intents from Gemini. An empty tq produces
            # a blacklist-only query ("" + historical + blacklist) = credit waste.
            if not tq or not tq.strip():
                log.warning("query_brain_empty_intent_skipped",
                            campaign_id=ctx.campaign_id,
                            note="Gemini returned empty translated_query — skipping.")
                continue
            # V24.1.3 FIX: Don't exact-match quote long conversational queries.
            # TASK 3 generates forum-style questions like:
            #   "How do I find trustworthy education consultants in India?"
            # Wrapping 15+ word sentences in quotes guarantees 0 Serper results
            # because no webpage contains that exact sentence. Short phrases
            # (≤ 6 words) benefit from quoting (precision). Longer phrases
            # benefit from unquoted natural-language matching (recall).
            # V26.0.4.2: Reduced from 10 to 6. Colloquial translations produce
            # 5-8 word everyday phrases like "Google Ads too expensive Kerala".
            # At the old 10-word threshold, these got exact-match quoted and
            # returned 0 results (nobody types that exact phrase). 6 words is
            # the sweet spot: "villa Muscat verified" (3) = quote for precision,
            # "reliable property agent Muscat reviews" (5) = quote OK,
            # "Google ads are eating my profits Kerala" (7) = DON'T quote.
            _word_count = len(tq.split())
            if _word_count <= 6:
                _tq_expr = f'"{tq}"'
            else:
                _tq_expr = tq
                log.info("query_brain_long_intent_unquoted",
                         words=_word_count, intent=tq[:80],
                         campaign_id=ctx.campaign_id,
                         note="Intent > 6 words — running as natural-language query "
                              "instead of exact-match to preserve Serper recall.")
            _bl = _deconflict_blacklist(f'{_tq_expr}{historical_str}', blacklist)
            smart_queries.append(f'{_tq_expr}{historical_str} {_bl}')
        log.info("query_brain_assembled",
                 count=len(ctx.intents), mode=_router_mode,
                 vector=ctx.sourcing_vector or "B2B",
                 is_consumer=_is_consumer)
    elif kw_str:
        if _is_consumer:
            # V24.3 (L2-4): B2C/D2C keyword fallback — use intent-template queries
            # instead of quoting raw bio words. Bio words like "private", "sale",
            # "villa" produce SEO directory results, not buyer signals.
            _geo_hint = ""
            _consumer_fallback_templates = [
                f'"looking for" OR "where can I find" OR "any recommendations" {blacklist}',
                f'"anyone selling" OR "direct owner" OR "pm me" {blacklist}',
                f'"advice needed" OR "help me find" OR "suggestion" {blacklist}',
            ]
            smart_queries.extend(_consumer_fallback_templates)
            log.info("query_brain_b2c_fallback_templates_used",
                     campaign_id=ctx.campaign_id,
                     template_count=len(_consumer_fallback_templates),
                     note="Consumer fallback: using intent templates instead of bio word split.")
        else:
            for kw in ctx.target_audience or []:
                if not kw or not kw.strip():
                    continue
                _bl = _deconflict_blacklist(f'("{kw}"){historical_str}', blacklist)
                smart_queries.append(f'("{kw}"){historical_str} {_bl}')

    for sd in ctx.symptom_dorks:
        if not sd or not sd.strip():
            log.warning("query_brain_empty_dork_skipped",
                        campaign_id=ctx.campaign_id,
                        note="Gemini returned empty symptom_dork — skipping.")
            continue
        _bl = _deconflict_blacklist(sd, blacklist)
        smart_queries.append(f"{sd} {_bl}")

    # ── V26.0.5: Strategy 1 — Platform Mining query injection ─────────────
    # For B2C consumer campaigns, the leads ARE on the competitor platforms
    # (agent profiles, property listings, directory pages). Pain-discovery
    # queries alone miss these entirely. This injects site: inclusion queries
    # targeting competitor listing platforms where real entities (agents,
    # brokers, sellers with names/emails/phones) are listed.
    #
    # From brainstorm:
    #   site:dubizzle.com.om "agent" "villa" "Muscat" "contact"
    #   site:propertyfinder.om agent profile
    #   site:dreoman.com agent
    #
    # These queries get NO time filter (qdr:m) at the Serper level because
    # agent profiles and listings are evergreen pages.
    # Platform mining for consumer archetypes, plus domain-driven platform
    # families (e.g. manufacturing B2B supplier directories) when profile says so.
    _domain_family = ""
    if isinstance(domain_profile, dict):
        _domain_family = str(domain_profile.get("domain_family") or "").strip().lower()
    _domain_wants_platforms = _domain_family in {
        "real_estate", "manufacturing", "construction", "healthcare",
        "professional_services", "hospitality",
    }
    if _is_consumer or _domain_wants_platforms:
        _platform_mining_queries = _generate_platform_mining_queries(
            ctx, bio, kw_str, vector_label, blacklist,
            strategy_plan=_strategy_plan,
            domain_profile=domain_profile if isinstance(domain_profile, dict) else None,
        )
        if _platform_mining_queries:
            smart_queries.extend(_platform_mining_queries)
            log.info(
                "query_brain_platform_mining_injected",
                campaign_id=ctx.campaign_id,
                count=len(_platform_mining_queries),
                queries=[q[:80] for q in _platform_mining_queries[:3]],
                domain_family=_domain_family or None,
                domain_driven=bool(_domain_wants_platforms and not _is_consumer),
                note="Strategy 1 (Platform Mining) queries added for "
                     "competitor platform lead discovery.",
            )

    # V25.4.0 Phase 2B: Inject buyer-language variants before syntax cleaning.
    smart_queries = _inject_buyer_language(smart_queries)

    # ── V26.0.1: Post-generation strategy-aware -site: strip ──────────────
    # The PLATFORM_MINING/COMPETITOR_TOUCHPOINT/PROFESSIONAL_NETWORK strategies
    # need certain platforms to remain unsuppressed. Gemini may inject -site:
    # exclusions into its generated dorks. Strip them here as a last-mile fix.
    # V26.0.3: Post-generation -site: strip — UNIVERSAL for all strategies.
    # Gemini's training priors may inject -site: exclusions for review/aggregator
    # sites even when the prompt doesn't ask for them. Strip exclusions for ALL
    # intelligence-valuable sites regardless of strategy.
    _strategy_keep_sites = [
        "g2.com", "capterra.com", "yelp.com", "trustpilot.com",
        "glassdoor.com", "linkedin.com", "mouthshut.com",
        "justdial.com", "indiamart.com", "sulekha.com",
    ]

    if _strategy_keep_sites and smart_queries:
        import re as _site_strip_re
        _strip_patterns = [
            _site_strip_re.compile(r'\s*-site:' + _site_strip_re.escape(domain) + r'\b', _site_strip_re.IGNORECASE)
            for domain in _strategy_keep_sites
        ]
        _stripped_count = 0
        _cleaned_queries = []
        for q in smart_queries:
            _original = q
            for pat in _strip_patterns:
                q = pat.sub('', q)
            if q != _original:
                _stripped_count += 1
            _cleaned_queries.append(q.strip())
        smart_queries = _cleaned_queries
        if _stripped_count:
            log.info("blacklist_strategy_post_strip",
                     strategy=ctx.strategy,
                     queries_affected=_stripped_count,
                     kept_sites=_strategy_keep_sites,
                     note="Stripped strategy-conflicting -site: exclusions from Gemini-generated dorks.")

    return [_clean_query_syntax(q) for q in smart_queries]
