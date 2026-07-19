"""V27 IntentDomainOrchestrator — single brain for domain + intent decisions.

Packaged under ``shared/`` so pipeline-main always has it (Docker COPY
``services/shared ./shared``). Do not rely solely on the optional top-level
``intelligence`` package for Cloud Run.

Design goals
------------
* One structured ``intent_profile`` consumed by produce, query governance,
  Serper noise filter, pre-filter, dispatch, entity extraction, and nourish.
* Fail-open: never abort a campaign cycle; degrade to conservative defaults.
* Backward compatible: active only when ``V27_INTELLIGENCE_ORCHESTRATOR=true``.
* No complexity explosion: consolidates scattered rules; does not invent new
  pipelines.

Legal-first: public Google index + public RSS/forums only. No login trespass.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

INTENT_PROFILE_VERSION = "intent-v1"

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t", "enabled"})
_FALSY = frozenset({"0", "false", "no", "off", "n", "f", "disabled", ""})


def _parse_bool_flag(raw: Any) -> bool | None:
    """Parse a flag value. Returns None when unset/unparseable (caller falls through)."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if raw == 1:
            return True
        if raw == 0:
            return False
        return None
    text = str(raw).strip().lower()
    # Tolerate accidental quotes from YAML/shell: "true" / 'true'
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("\"", "'"):
        text = text[1:-1].strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def env_v27_flag(env: Mapping[str, str] | None = None) -> Tuple[bool, str]:
    """Read V27_INTELLIGENCE_ORCHESTRATOR from env. Returns (enabled, raw_value)."""
    source = env if env is not None else os.environ
    raw = source.get("V27_INTELLIGENCE_ORCHESTRATOR")
    if raw is None:
        # Some platforms inject empty string when unset in overlays
        return False, ""
    parsed = _parse_bool_flag(raw)
    if parsed is None:
        # Unrecognized value → fail-open to False (legacy path)
        return False, str(raw)
    return parsed, str(raw)


def is_v27_orchestrator_enabled(
    campaign: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return True when V27 orchestrator should run.

    Precedence (explicit values only; None/missing falls through):
      1. campaign.flags.v27_intelligence_orchestrator (or feature_flags)
      2. campaign.v27_intelligence_orchestrator
      3. env V27_INTELLIGENCE_ORCHESTRATOR (default false)

    Note: A campaign flag of null/missing does NOT suppress the env var.
    Only an explicit false disables when env is true.
    """
    if campaign and isinstance(campaign, Mapping):
        flags = campaign.get("flags")
        if not isinstance(flags, Mapping):
            flags = campaign.get("feature_flags")
        if isinstance(flags, Mapping) and "v27_intelligence_orchestrator" in flags:
            parsed = _parse_bool_flag(flags.get("v27_intelligence_orchestrator"))
            if parsed is not None:
                return parsed
            # null / unparseable → fall through to env (do not treat as False)
        if "v27_intelligence_orchestrator" in campaign:
            parsed = _parse_bool_flag(campaign.get("v27_intelligence_orchestrator"))
            if parsed is not None:
                return parsed
    enabled, _raw = env_v27_flag(env)
    return enabled


def v27_flag_diagnostics(
    campaign: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Structured diagnostics for produce/dispatch skip logs."""
    env_on, env_raw = env_v27_flag(env)
    campaign_val: Any = None
    campaign_source = "none"
    if campaign and isinstance(campaign, Mapping):
        flags = campaign.get("flags")
        if not isinstance(flags, Mapping):
            flags = campaign.get("feature_flags")
        if isinstance(flags, Mapping) and "v27_intelligence_orchestrator" in flags:
            campaign_val = flags.get("v27_intelligence_orchestrator")
            campaign_source = "flags"
        elif "v27_intelligence_orchestrator" in campaign:
            campaign_val = campaign.get("v27_intelligence_orchestrator")
            campaign_source = "campaign_top_level"
    return {
        "enabled": is_v27_orchestrator_enabled(campaign, env=env),
        "env_raw": env_raw,
        "env_enabled": env_on,
        "campaign_flag_source": campaign_source,
        "campaign_flag_raw": campaign_val,
    }


# ---------------------------------------------------------------------------
# Public channel matrix — always admissible under V27 (no hard domain bans)
# ---------------------------------------------------------------------------

PUBLIC_CHANNELS_ALWAYS_ADMIT: frozenset[str] = frozenset({
    # Review / software directories
    "g2.com", "capterra.com", "trustpilot.com", "yelp.com", "glassdoor.com",
    "mouthshut.com", "clutch.co", "goodfirms.co",
    # Community / Q&A
    "reddit.com", "quora.com", "news.ycombinator.com", "stackexchange.com",
    "stackoverflow.com", "indiehackers.com",
    # Public social (snippet / public posts only)
    "linkedin.com", "facebook.com", "youtube.com", "x.com", "twitter.com",
    # Property / classified directories
    "bayut.com", "propertyfinder.com", "dubizzle.com", "olx.com",
    "zillow.com", "realtor.com", "rightmove.co.uk", "99acres.com",
    "magicbricks.com", "housing.com", "craigslist.org", "gumtree.com",
    "justdial.com", "indiamart.com", "sulekha.com", "thomasnet.com",
    # Google surfaces
    "google.com", "maps.google.com",
})

# Path / role patterns that indicate competitor marketing or non-lead pages.
# These are the ONLY default hard exclusions under V27 (not whole domains).
DEFAULT_EXCLUDE_PATH_PATTERNS: tuple[str, ...] = (
    "/author/", "/authors/", "/pricing", "/price", "/legal", "/terms",
    "/privacy", "/login", "/signin", "/sign-in", "/signup", "/sign-up",
    "/careers", "/jobs/", "/job/", "/about-us", "/our-services",
    "/case-studies", "/case_studies", "/portfolio",
)

DEFAULT_EXCLUDE_SNIPPET_PATTERNS: tuple[str, ...] = (
    "sign in", "access denied", "forgot password", "please enable cookies",
    "log in to continue", "create an account to continue",
)

# True infrastructure / non-lead noise — never public lead channels.
# Kept narrow so we do not reintroduce G2-style channel bans.
INFRASTRUCTURE_NOISE_DOMAINS: frozenset[str] = frozenset({
    "zoominfo.com", "apollo.io", "clearbit.com", "hunter.io",
    "ssrn.com", "researchgate.net", "semanticscholar.org",
    "wikipedia.org", "wikia.com", "fandom.com",
})

# Content-farm hosts: under V27 admitted only when news is in channel_priority
# or use_case is EVENT_TRIGGER_MONITOR — otherwise soft-dropped (not hard ban
# of the entire news channel class when strategy needs it).
CONTENT_FARM_DOMAINS: frozenset[str] = frozenset({
    "buzzfeed.com", "wikihow.com", "bbc.com", "bbc.co.uk", "cnn.com",
    "ndtv.com", "timesofindia.indiatimes.com", "gulfnews.com", "khaleejtimes.com",
    "huffpost.com", "foxnews.com", "dailymail.co.uk", "nypost.com",
    "theguardian.com", "aljazeera.com", "abcnews.go.com", "nbcnews.com",
    "cbsnews.com", "usatoday.com", "apnews.com", "vice.com", "vox.com",
    "theverge.com", "mashable.com", "boredpanda.com", "ranker.com",
    "screenrant.com", "gamerant.com", "cbr.com", "hindustantimes.com",
    "indiatoday.in", "firstpost.com", "news18.com", "thehindu.com",
})

# Business news exceptions always admissible for B2B event triggers.
BUSINESS_NEWS_ALWAYS: frozenset[str] = frozenset({
    "bloomberg.com", "businessinsider.com", "insider.com",
    "reuters.com", "cnbc.com", "livemint.com",
    "washingtonpost.com", "nytimes.com",
})

# Use-case catalog
USE_CASE_PLATFORM_BUYER_MINING = "PLATFORM_BUYER_MINING"
USE_CASE_CAC_COMPETITOR_TOUCHPOINT = "CAC_COMPETITOR_TOUCHPOINT"
USE_CASE_BRAND_NARRATIVE = "BRAND_NARRATIVE_OUTREACH"
USE_CASE_COLLOQUIAL_PAIN = "COLLOQUIAL_PAIN_DISCOVERY"
USE_CASE_EVENT_TRIGGER = "EVENT_TRIGGER_MONITOR"
USE_CASE_PROFESSIONAL_NETWORK = "PROFESSIONAL_NETWORK_OUTREACH"
USE_CASE_SCAM_RECOVERY = "SCAM_RECOVERY_PLATFORM_MINING"


# ---------------------------------------------------------------------------
# IntentProfile
# ---------------------------------------------------------------------------

@dataclass
class IntentProfile:
    """Unified intent + domain decision object for the full pipeline."""

    version: str = INTENT_PROFILE_VERSION
    use_case: str = USE_CASE_COLLOQUIAL_PAIN
    buyer_intent: str = "medium"  # high | medium | low | mixed
    primary_strategy: str = "COLLOQUIAL_DISCOVERY"
    secondary_strategy: str = "NONE"
    domain_family: str = "general_services"
    sourcing_vector: str = "B2B"
    platform_mining_level: str = "optional"  # force | prefer | optional | none
    liquidity_level: str = "medium"
    low_liquidity_market: bool = False
    force_geo_global_fallback: bool = False
    force_platform_mining: bool = False
    min_platform_queries: int = 0
    max_site_exclusions: int = 6
    negative_intent_cap_ratio: float = 0.30
    channel_priority: list[str] = field(default_factory=list)
    always_admit_channels: list[str] = field(default_factory=list)
    competitor_exclusion_mode: str = "path_and_role"  # path_and_role | none
    exclude_path_patterns: list[str] = field(default_factory=list)
    exclude_snippet_patterns: list[str] = field(default_factory=list)
    never_block_domains: list[str] = field(default_factory=list)
    nourish_depth: str = "standard"  # deep | standard | entity_first | snippet
    nourish_plan: dict[str, Any] = field(default_factory=dict)
    entity_extraction_enabled: bool = False
    soft_directory_prefilter: bool = False
    admit_news_channels: bool = False
    query_hints: list[str] = field(default_factory=list)
    platform_targets: list[str] = field(default_factory=list)
    decision_reasons: list[str] = field(default_factory=list)
    profile_confidence: str = "medium"
    orchestrator_active: bool = True
    built_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "IntentProfile":
        if not data or not isinstance(data, Mapping):
            return cls(orchestrator_active=False, built_at=_utcnow_iso())
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)  # type: ignore[arg-type]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(*parts: Any) -> str:
    chunks: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (list, tuple)):
            chunks.append(" ".join(str(x) for x in part if x))
        else:
            chunks.append(str(part))
    return " ".join(chunks).lower()


def _domain_from_profile(domain_profile: Mapping[str, Any] | None) -> tuple[str, str, bool, str]:
    if not isinstance(domain_profile, Mapping):
        return "general_services", "medium", False, "low"
    family = str(domain_profile.get("domain_family") or "general_services").strip().lower()
    liquidity = str(domain_profile.get("liquidity_level") or "medium").strip().lower()
    low = bool(
        domain_profile.get("low_liquidity_market")
        or liquidity == "low"
    )
    conf = str(domain_profile.get("profile_confidence") or "").strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "low" if domain_profile.get("thin_campaign") else "medium"
    return family or "general_services", liquidity or "medium", low, conf


def _primary_strategy(campaign: Mapping[str, Any]) -> tuple[str, str]:
    strategy = campaign.get("intelligence_strategy") or {}
    if not isinstance(strategy, Mapping):
        strategy = {}
    primary = str(strategy.get("primary") or "").upper().strip()
    secondary = str(strategy.get("secondary") or "NONE").upper().strip() or "NONE"
    return primary, secondary


def _platform_targets(campaign: Mapping[str, Any], domain_profile: Mapping[str, Any] | None) -> list[str]:
    strategy = campaign.get("intelligence_strategy") or {}
    targets: list[str] = []
    if isinstance(strategy, Mapping):
        raw = strategy.get("platform_targets") or []
        if isinstance(raw, list):
            targets.extend(str(t).strip() for t in raw if str(t).strip())
    if isinstance(domain_profile, Mapping):
        for hint in domain_profile.get("preferred_query_hints") or []:
            text = str(hint).strip()
            if text and text not in targets:
                targets.append(text)
    return targets[:12]


# ---------------------------------------------------------------------------
# Use-case classification (real-life heuristics)
# ---------------------------------------------------------------------------

_SCAM_TERMS = (
    "scam", "fraud", "fake agent", "fake listing", "cheated", "ripped off",
    "dishonest", "misleading listing", "hidden fees", "broker scam",
)
_CAC_TERMS = (
    "cac", "customer acquisition", "cost per lead", "cost per acquisition",
    "churn", "switching from", "alternative to", "looking for alternative",
    "too expensive", "pricing increase", "vendor lock", "roi of",
    "competitor review", "vs ", " versus ",
)
_BRAND_TERMS = (
    "brand narrative", "brand positioning", "brand identity", "brand architecture",
    "brand strategy", "fmcg", "storytelling", "creative agency", "brand voice",
    "retail marketing", "brand book",
)
_EVENT_TERMS = (
    "funding", "raised series", "series a", "series b", "hiring", "expansion",
    "acquired", "merger", "ipo", "regulatory", "compliance deadline",
)
_PROF_TERMS = (
    "conference", "speaker", "linkedin", "c-level", "decision maker",
    "vp of", "head of sales", "procurement",
)
_REAL_ESTATE_TERMS = (
    "real estate", "property", "villa", "apartment", "broker", "agent",
    "listing", "bayut", "propertyfinder", "dubizzle", "rent", "landlord",
)


def classify_use_case(
    corpus: str,
    *,
    primary_strategy: str,
    domain_family: str,
    sourcing_vector: str,
) -> tuple[str, list[str]]:
    """Classify real-life use case. Returns (use_case, reasons)."""
    reasons: list[str] = []
    text = corpus or ""
    family = (domain_family or "").lower()
    strategy = (primary_strategy or "").upper()
    vector = (sourcing_vector or "").upper()

    if any(t in text for t in _SCAM_TERMS):
        reasons.append("scam_or_fraud_language")
        return USE_CASE_SCAM_RECOVERY, reasons

    if any(t in text for t in _CAC_TERMS) or strategy == "COMPETITOR_TOUCHPOINT":
        if strategy == "COMPETITOR_TOUCHPOINT":
            reasons.append("strategy_competitor_touchpoint")
        else:
            reasons.append("cac_or_switch_language")
        return USE_CASE_CAC_COMPETITOR_TOUCHPOINT, reasons

    if any(t in text for t in _BRAND_TERMS) or family == "marketing_agency":
        reasons.append("brand_or_marketing_agency")
        return USE_CASE_BRAND_NARRATIVE, reasons

    if any(t in text for t in _EVENT_TERMS) or strategy == "EVENT_TRIGGER":
        reasons.append("event_or_funding_language")
        return USE_CASE_EVENT_TRIGGER, reasons

    if strategy == "PROFESSIONAL_NETWORK" or any(t in text for t in _PROF_TERMS):
        reasons.append("professional_network_signals")
        return USE_CASE_PROFESSIONAL_NETWORK, reasons

    if (
        strategy == "PLATFORM_MINING"
        or family == "real_estate"
        or (vector in {"B2C", "D2C", "B2B2C"} and any(t in text for t in _REAL_ESTATE_TERMS))
    ):
        reasons.append("platform_mining_or_real_estate")
        return USE_CASE_PLATFORM_BUYER_MINING, reasons

    if strategy == "COLLOQUIAL_DISCOVERY" or not strategy:
        reasons.append("default_colloquial")
        return USE_CASE_COLLOQUIAL_PAIN, reasons

    reasons.append(f"strategy_fallback:{strategy or 'NONE'}")
    return USE_CASE_COLLOQUIAL_PAIN, reasons


def _channel_priority_for_use_case(
    use_case: str,
    domain_family: str,
    sourcing_vector: str,
) -> list[str]:
    family = (domain_family or "").lower()
    vector = (sourcing_vector or "").upper()

    if use_case in (USE_CASE_SCAM_RECOVERY, USE_CASE_PLATFORM_BUYER_MINING):
        if family == "real_estate":
            return [
                "directories", "google_reviews", "trustpilot", "reddit",
                "quora", "facebook", "news",
            ]
        if family in ("saas", "marketing_agency") or vector == "B2B":
            return [
                "g2", "capterra", "trustpilot", "linkedin", "reddit",
                "quora", "news", "directories",
            ]
        return [
            "directories", "trustpilot", "yelp", "reddit", "quora",
            "google_reviews", "facebook",
        ]

    if use_case == USE_CASE_CAC_COMPETITOR_TOUCHPOINT:
        return [
            "g2", "capterra", "trustpilot", "reddit", "quora",
            "google_reviews", "linkedin", "news",
        ]

    if use_case == USE_CASE_BRAND_NARRATIVE:
        return [
            "linkedin", "reddit", "quora", "news", "trustpilot",
            "youtube", "directories",
        ]

    if use_case == USE_CASE_EVENT_TRIGGER:
        return ["news", "linkedin", "job_posts", "rss", "reddit", "hackernews"]

    if use_case == USE_CASE_PROFESSIONAL_NETWORK:
        return ["linkedin", "job_posts", "rss", "reddit", "hackernews", "news"]

    # colloquial default
    if vector in {"B2C", "D2C", "B2B2C"}:
        return ["reddit", "quora", "trustpilot", "directories", "facebook", "youtube"]
    return ["reddit", "linkedin", "quora", "g2", "trustpilot", "news", "hackernews"]


def _strategy_for_use_case(use_case: str, primary: str, secondary: str) -> tuple[str, str]:
    mapping = {
        USE_CASE_SCAM_RECOVERY: ("PLATFORM_MINING", "COLLOQUIAL_DISCOVERY"),
        USE_CASE_PLATFORM_BUYER_MINING: ("PLATFORM_MINING", "COLLOQUIAL_DISCOVERY"),
        USE_CASE_CAC_COMPETITOR_TOUCHPOINT: ("COMPETITOR_TOUCHPOINT", "COLLOQUIAL_DISCOVERY"),
        USE_CASE_BRAND_NARRATIVE: ("COLLOQUIAL_DISCOVERY", "PROFESSIONAL_NETWORK"),
        USE_CASE_EVENT_TRIGGER: ("EVENT_TRIGGER", "COLLOQUIAL_DISCOVERY"),
        USE_CASE_PROFESSIONAL_NETWORK: ("PROFESSIONAL_NETWORK", "EVENT_TRIGGER"),
        USE_CASE_COLLOQUIAL_PAIN: ("COLLOQUIAL_DISCOVERY", "NONE"),
    }
    default = mapping.get(use_case, ("COLLOQUIAL_DISCOVERY", "NONE"))
    # Prefer explicit campaign strategy when already set and compatible
    if primary and primary not in ("", "NONE", "UNKNOWN"):
        # Override only when use-case strongly implies a different primary
        if use_case in (USE_CASE_SCAM_RECOVERY, USE_CASE_PLATFORM_BUYER_MINING) and primary != "PLATFORM_MINING":
            return default
        if use_case == USE_CASE_CAC_COMPETITOR_TOUCHPOINT and primary != "COMPETITOR_TOUCHPOINT":
            return default
        return primary, secondary or default[1]
    return default


def _platform_mining_level(use_case: str, force: bool) -> str:
    if force or use_case in (USE_CASE_SCAM_RECOVERY, USE_CASE_PLATFORM_BUYER_MINING):
        return "force"
    if use_case == USE_CASE_CAC_COMPETITOR_TOUCHPOINT:
        return "prefer"
    if use_case == USE_CASE_BRAND_NARRATIVE:
        return "optional"
    if use_case == USE_CASE_EVENT_TRIGGER:
        return "none"
    return "optional"


def _nourish_for_use_case(use_case: str, platform_level: str) -> tuple[str, dict[str, Any], bool]:
    """Return nourish_depth, nourish_plan, entity_extraction_enabled.

    Use-case wins over platform_level so brand-narrative / event campaigns
    do not inherit entity_first just because platform mining is preferred.
    """
    if use_case in (USE_CASE_SCAM_RECOVERY, USE_CASE_PLATFORM_BUYER_MINING):
        plan = {
            "entity_extraction": True,
            "public_contact_harvest": True,
            "deep_context": True,
            "intelligence_mesh": True,
            "priority": "realtime",
            "status_on_thin": "enrichment_pending",
            "required_fields": ["company_name", "decision_maker_name", "contact_endpoints"],
        }
        return "entity_first", plan, True

    if use_case == USE_CASE_CAC_COMPETITOR_TOUCHPOINT:
        plan = {
            "entity_extraction": True,
            "public_contact_harvest": True,
            "deep_context": True,
            "intelligence_mesh": True,
            "priority": "realtime",
            "status_on_thin": "enrichment_pending",
            "required_fields": ["company_name", "pain_point", "intent_signal"],
        }
        return "deep", plan, True

    if use_case == USE_CASE_EVENT_TRIGGER:
        plan = {
            "entity_extraction": False,
            "public_contact_harvest": True,
            "deep_context": True,
            "intelligence_mesh": True,
            "priority": "batch",
            "status_on_thin": "enrichment_pending",
            "required_fields": ["company_name", "intent_signal"],
        }
        return "deep", plan, False

    if use_case == USE_CASE_BRAND_NARRATIVE:
        plan = {
            "entity_extraction": False,
            "public_contact_harvest": True,
            "deep_context": True,
            "intelligence_mesh": False,
            "priority": "batch",
            "status_on_thin": "enrichment_pending",
            "required_fields": ["company_name", "pain_point"],
        }
        return "standard", plan, False

    plan = {
        "entity_extraction": False,
        "public_contact_harvest": True,
        "deep_context": True,
        "intelligence_mesh": False,
        "priority": "batch",
        "status_on_thin": "enrichment_pending",
        "required_fields": ["pain_point"],
    }
    return "standard", plan, False


def _buyer_intent_level(use_case: str, corpus: str) -> str:
    if use_case in (USE_CASE_SCAM_RECOVERY, USE_CASE_CAC_COMPETITOR_TOUCHPOINT):
        return "high"
    if use_case in (USE_CASE_PLATFORM_BUYER_MINING, USE_CASE_EVENT_TRIGGER):
        return "high" if any(t in corpus for t in ("looking for", "need", "urgent", "asap", "recommend")) else "medium"
    if use_case == USE_CASE_BRAND_NARRATIVE:
        return "mixed"
    return "medium"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_intent_profile(
    campaign: Mapping[str, Any] | None,
    domain_profile: Mapping[str, Any] | None = None,
    *,
    force_enabled: bool | None = None,
) -> IntentProfile:
    """Build a unified intent_profile from campaign + domain context.

    Fail-open: never raises; returns a safe profile with decision_reasons.
    When orchestrator is disabled and force_enabled is not True, returns a
    profile with ``orchestrator_active=False`` (callers should use legacy path).
    """
    campaign = campaign if isinstance(campaign, Mapping) else {}
    domain_profile = domain_profile if isinstance(domain_profile, Mapping) else (
        campaign.get("system_domain_profile")
        if isinstance(campaign.get("system_domain_profile"), Mapping)
        else {}
    )

    enabled = True if force_enabled is True else (
        False if force_enabled is False else is_v27_orchestrator_enabled(campaign=campaign)
    )

    family, liquidity, low_liq, conf = _domain_from_profile(domain_profile)
    primary, secondary = _primary_strategy(campaign)
    vector = str(
        campaign.get("sourcing_vector")
        or (domain_profile or {}).get("recommended_sourcing_vector")
        or "B2B"
    ).upper().strip() or "B2B"

    corpus = _clean_text(
        campaign.get("name"),
        campaign.get("bio"),
        campaign.get("effective_bio"),
        campaign.get("pain_point"),
        campaign.get("keywords"),
        campaign.get("persona_keywords"),
        campaign.get("persona_bio"),
        campaign.get("target_angle_hook"),
        campaign.get("persona_name"),
        (campaign.get("intelligence_strategy") or {}).get("vocabulary_notes")
        if isinstance(campaign.get("intelligence_strategy"), Mapping)
        else "",
    )

    use_case, uc_reasons = classify_use_case(
        corpus,
        primary_strategy=primary,
        domain_family=family,
        sourcing_vector=vector,
    )
    resolved_primary, resolved_secondary = _strategy_for_use_case(use_case, primary, secondary)

    # Force platform mining only for true directory/platform use cases.
    # SaaS / marketing get "prefer" via use-case (CAC / brand), not force —
    # avoids entity_first nourish on brand-narrative campaigns.
    _force_platform_families = {"real_estate", "manufacturing", "construction"}
    force_platform = (
        use_case in (USE_CASE_SCAM_RECOVERY, USE_CASE_PLATFORM_BUYER_MINING)
        or resolved_primary == "PLATFORM_MINING"
        or family in _force_platform_families
    )
    platform_level = _platform_mining_level(use_case, force_platform)
    if (
        not force_platform
        and family in {"saas", "healthcare", "professional_services", "hospitality", "marketing_agency"}
        and platform_level == "optional"
    ):
        platform_level = "prefer"
    channels = _channel_priority_for_use_case(use_case, family, vector)
    nourish_depth, nourish_plan, entity_on = _nourish_for_use_case(use_case, platform_level)
    admit_news = (
        use_case == USE_CASE_EVENT_TRIGGER
        or "news" in channels
        or resolved_primary == "EVENT_TRIGGER"
    )

    max_sites = 4 if low_liq else 6
    if platform_level == "force":
        neg_ratio = 0.20 if low_liq else 0.25
        min_platform = 4
    elif platform_level == "prefer":
        neg_ratio = 0.25
        min_platform = 2
    else:
        neg_ratio = 0.30 if vector in {"B2C", "D2C", "B2B2C"} else 0.35
        if low_liq:
            neg_ratio = min(neg_ratio, 0.25)
        min_platform = 0

    force_geo = bool(low_liq) or platform_level == "force"
    soft_dir = platform_level in ("force", "prefer") or use_case == USE_CASE_CAC_COMPETITOR_TOUCHPOINT
    platforms = _platform_targets(campaign, domain_profile)

    # Default platform seeds by family when empty
    if not platforms and family == "real_estate":
        platforms = ["bayut.com", "propertyfinder.com", "dubizzle.com", "olx.com"]
    elif not platforms and family == "saas":
        platforms = ["g2.com", "capterra.com", "trustpilot.com"]
    elif not platforms and family == "marketing_agency":
        platforms = ["linkedin.com", "clutch.co", "reddit.com"]
    elif not platforms and family == "education":
        # V27: education sub-pattern hosts (B2C student/parent) when profile sparse.
        try:
            from shared.education_profiles import (  # type: ignore[import]
                education_platform_hosts,
                normalize_education_sub_pattern,
                resolve_education_profile,
            )
            if isinstance(domain_profile, Mapping) and domain_profile.get("platform_hosts"):
                platforms = [
                    str(h).strip() for h in domain_profile.get("platform_hosts") or []
                    if str(h).strip()
                ][:6]
            else:
                sub = "general_education"
                is_b2b = vector == "B2B"
                if isinstance(domain_profile, Mapping):
                    sub = normalize_education_sub_pattern(
                        domain_profile.get("education_sub_pattern")
                    )
                    if "is_b2b_education" in domain_profile:
                        is_b2b = bool(domain_profile.get("is_b2b_education"))
                else:
                    edu = resolve_education_profile(campaign, sourcing_vector=vector)
                    sub = edu.get("education_sub_pattern") or sub
                    is_b2b = bool(edu.get("is_b2b_education"))
                platforms = education_platform_hosts(sub, is_b2b=is_b2b)[:6]
        except Exception:
            platforms = ["reddit.com", "quora.com", "youtube.com", "shiksha.com"]

    query_hints: list[str] = []
    if isinstance(domain_profile, Mapping):
        query_hints = [
            str(h).strip() for h in (domain_profile.get("preferred_query_hints") or [])
            if str(h).strip()
        ][:8]
        # Prefer education-resolved hints over empty / legacy teacher packs.
        if family == "education" and not query_hints:
            try:
                from shared.education_profiles import resolve_education_profile  # type: ignore[import]
                edu = resolve_education_profile(campaign, sourcing_vector=vector)
                query_hints = list(edu.get("preferred_query_hints") or [])[:8]
            except Exception:
                pass

    reasons = list(uc_reasons)
    reasons.append(f"domain_family={family}")
    reasons.append(f"liquidity={liquidity}")
    reasons.append(f"platform_level={platform_level}")
    reasons.append(f"vector={vector}")
    if not enabled:
        reasons.append("orchestrator_flag_off")

    profile = IntentProfile(
        version=INTENT_PROFILE_VERSION,
        use_case=use_case,
        buyer_intent=_buyer_intent_level(use_case, corpus),
        primary_strategy=resolved_primary,
        secondary_strategy=resolved_secondary,
        domain_family=family,
        sourcing_vector=vector,
        platform_mining_level=platform_level,
        liquidity_level=liquidity,
        low_liquidity_market=low_liq,
        force_geo_global_fallback=force_geo,
        force_platform_mining=platform_level == "force",
        min_platform_queries=min_platform,
        max_site_exclusions=max_sites,
        negative_intent_cap_ratio=neg_ratio,
        channel_priority=channels,
        always_admit_channels=sorted(PUBLIC_CHANNELS_ALWAYS_ADMIT),
        competitor_exclusion_mode="path_and_role",
        exclude_path_patterns=list(DEFAULT_EXCLUDE_PATH_PATTERNS),
        exclude_snippet_patterns=list(DEFAULT_EXCLUDE_SNIPPET_PATTERNS),
        never_block_domains=sorted(PUBLIC_CHANNELS_ALWAYS_ADMIT),
        nourish_depth=nourish_depth,
        nourish_plan=nourish_plan,
        entity_extraction_enabled=entity_on,
        soft_directory_prefilter=soft_dir,
        admit_news_channels=admit_news,
        query_hints=query_hints,
        platform_targets=platforms,
        decision_reasons=reasons,
        profile_confidence=conf,
        orchestrator_active=enabled,
        built_at=_utcnow_iso(),
    )
    return profile


# ---------------------------------------------------------------------------
# Channel admission / result filtering (consumed by serper noise filter)
# ---------------------------------------------------------------------------

def _root_domain(url: str) -> str:
    try:
        raw = url or ""
        if "://" not in raw:
            raw = "http://" + raw
        netloc = urlparse(raw).netloc.lower().replace("www.", "")
        return netloc
    except Exception:
        return ""


def channel_is_admissible(domain: str, profile: IntentProfile | Mapping[str, Any] | None) -> bool:
    """True if domain is a public channel that must not be hard-blocked."""
    d = (domain or "").lower().replace("www.", "")
    if not d:
        return False
    if d in PUBLIC_CHANNELS_ALWAYS_ADMIT:
        return True
    if d in BUSINESS_NEWS_ALWAYS:
        return True
    # subdomain of always-admit
    for ch in PUBLIC_CHANNELS_ALWAYS_ADMIT:
        if d == ch or d.endswith("." + ch):
            return True
    if profile is None:
        return False
    never_block = (
        profile.never_block_domains
        if isinstance(profile, IntentProfile)
        else list(profile.get("never_block_domains") or [])
    )
    for ch in never_block:
        ch = str(ch).lower()
        if d == ch or d.endswith("." + ch):
            return True
    return False


def should_hard_drop_result(
    result: Mapping[str, Any],
    profile: IntentProfile | Mapping[str, Any] | None,
    *,
    legacy_enterprise_domains: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """Decide whether a Serper organic result should be hard-dropped.

    Under V27 (profile.orchestrator_active):
      * Public channels never hard-dropped for being G2/Capterra/etc.
      * Path/snippet competitor-author rules may still drop.
      * Infrastructure noise may drop.
      * Content farms drop only when news not admitted.

    Fail-open: returns (False, reason) when uncertain.
    """
    link = str(result.get("link") or result.get("url") or "")
    snippet = str(result.get("snippet") or "").lower()
    title = str(result.get("title") or "").lower()
    domain = _root_domain(link)
    if not domain:
        return False, "no_domain"

    active = True
    admit_news = False
    path_patterns = list(DEFAULT_EXCLUDE_PATH_PATTERNS)
    snippet_patterns = list(DEFAULT_EXCLUDE_SNIPPET_PATTERNS)

    if isinstance(profile, IntentProfile):
        active = bool(profile.orchestrator_active)
        admit_news = bool(profile.admit_news_channels)
        path_patterns = list(profile.exclude_path_patterns or path_patterns)
        snippet_patterns = list(profile.exclude_snippet_patterns or snippet_patterns)
    elif isinstance(profile, Mapping):
        active = bool(profile.get("orchestrator_active", True))
        admit_news = bool(profile.get("admit_news_channels"))
        path_patterns = list(profile.get("exclude_path_patterns") or path_patterns)
        snippet_patterns = list(profile.get("exclude_snippet_patterns") or snippet_patterns)

    if not active:
        # Legacy path: caller handles full legacy filter; we only advise.
        return False, "orchestrator_inactive"

    # Always keep public lead channels unless path/snippet competitor rules fire.
    is_channel = channel_is_admissible(domain, profile)

    # Infrastructure noise (not a public lead channel)
    for noise in INFRASTRUCTURE_NOISE_DOMAINS:
        if domain == noise or domain.endswith("." + noise):
            if not is_channel:
                return True, "infrastructure_noise"

    # Content farm / general news
    if domain in CONTENT_FARM_DOMAINS or any(domain.endswith("." + d) for d in CONTENT_FARM_DOMAINS):
        if domain in BUSINESS_NEWS_ALWAYS or any(domain.endswith("." + d) for d in BUSINESS_NEWS_ALWAYS):
            return False, "business_news_admit"
        if admit_news:
            return False, "news_admitted_by_intent"
        if is_channel:
            return False, "channel_admit"
        return True, "content_farm_not_in_intent"

    # Path-based competitor / author exclusion (applies even on channels)
    try:
        path = urlparse(link if "://" in link else "http://" + link).path.lower()
    except Exception:
        path = ""
    for pat in path_patterns:
        if pat and pat in path:
            # Do not drop entity listing roots — only author/marketing paths
            return True, f"path_exclude:{pat}"

    for pat in snippet_patterns:
        if pat and pat in snippet:
            return True, f"snippet_exclude:{pat}"

    # Legacy enterprise mega-vendors (ibm/amazon) — soft: drop only non-channel
    for ent in (legacy_enterprise_domains or ()):
        ent_d = str(ent).lower().replace("www.", "")
        if not ent_d:
            continue
        if domain == ent_d or domain.endswith("." + ent_d):
            if is_channel or ent_d in PUBLIC_CHANNELS_ALWAYS_ADMIT:
                return False, "enterprise_but_channel_admit"
            # Keep amazon/ibm hard-drop as marketplace noise, not lead channel
            if ent_d in ("g2.com", "capterra.com", "trustpilot.com", "yelp.com"):
                return False, "review_channel_admit"
            return True, f"legacy_enterprise:{ent_d}"

    # Megathread titles — low signal
    _mega = (
        "megathread", "mega thread", "daily discussion", "weekly roundup",
        "weekly thread", "monthly roundup", "open thread", "free talk",
    )
    if any(m in title for m in _mega):
        return True, "megathread_title"

    return False, "admit"


def nourish_plan_for_profile(
    profile: IntentProfile | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return standardized nourish plan (fail-open default)."""
    if isinstance(profile, IntentProfile):
        base = dict(profile.nourish_plan or {})
        base.setdefault("depth", profile.nourish_depth)
        base.setdefault("entity_extraction", profile.entity_extraction_enabled)
        return base
    if isinstance(profile, Mapping):
        base = dict(profile.get("nourish_plan") or {})
        base.setdefault("depth", profile.get("nourish_depth") or "standard")
        base.setdefault("entity_extraction", bool(profile.get("entity_extraction_enabled")))
        return base
    return {
        "depth": "standard",
        "entity_extraction": False,
        "public_contact_harvest": True,
        "deep_context": True,
        "intelligence_mesh": False,
        "priority": "batch",
        "status_on_thin": "enrichment_pending",
        "required_fields": ["pain_point"],
    }


def funnel_snapshot(
    *,
    intent_profile: IntentProfile | Mapping[str, Any] | None = None,
    queries_executed: int = 0,
    raw_hits: int = 0,
    after_noise: int = 0,
    after_stale: int = 0,
    queued: int = 0,
    geo_fallbacks_attempted: int = 0,
    geo_fallbacks_succeeded: int = 0,
    platform_queries_executed: int = 0,
    noise_dropped: int = 0,
    channel_admitted: int = 0,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build campaign ``last_cycle_funnel`` payload (additive, BC-safe)."""
    use_case = None
    strategy = None
    if isinstance(intent_profile, IntentProfile):
        use_case = intent_profile.use_case
        strategy = intent_profile.primary_strategy
        active = intent_profile.orchestrator_active
    elif isinstance(intent_profile, Mapping):
        use_case = intent_profile.get("use_case")
        strategy = intent_profile.get("primary_strategy")
        active = bool(intent_profile.get("orchestrator_active"))
    else:
        active = False

    snap: dict[str, Any] = {
        "version": "funnel-v1",
        "orchestrator_active": active,
        "use_case": use_case,
        "primary_strategy": strategy,
        "queries_executed": int(queries_executed),
        "raw_hits": int(raw_hits),
        "after_noise": int(after_noise),
        "after_stale": int(after_stale),
        "queued": int(queued),
        "geo_fallbacks_attempted": int(geo_fallbacks_attempted),
        "geo_fallbacks_succeeded": int(geo_fallbacks_succeeded),
        "platform_queries_executed": int(platform_queries_executed),
        "noise_dropped": int(noise_dropped),
        "channel_admitted": int(channel_admitted),
        "recorded_at": _utcnow_iso(),
    }
    if extra:
        for k, v in extra.items():
            if k not in snap:
                snap[k] = v
    return snap


def apply_intent_to_governance_stats(
    profile: IntentProfile | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Extract governance knobs from intent profile for query_governance."""
    if isinstance(profile, IntentProfile) and profile.orchestrator_active:
        return {
            "max_site_exclusions": profile.max_site_exclusions,
            "negative_intent_cap_ratio": profile.negative_intent_cap_ratio,
            "force_platform_mining": profile.force_platform_mining,
            "min_platform_queries": profile.min_platform_queries,
            "primary_strategy": profile.primary_strategy,
            "low_liquidity": profile.low_liquidity_market,
            "use_case": profile.use_case,
        }
    if isinstance(profile, Mapping) and profile.get("orchestrator_active"):
        return {
            "max_site_exclusions": int(profile.get("max_site_exclusions") or 6),
            "negative_intent_cap_ratio": float(profile.get("negative_intent_cap_ratio") or 0.30),
            "force_platform_mining": bool(profile.get("force_platform_mining")),
            "min_platform_queries": int(profile.get("min_platform_queries") or 0),
            "primary_strategy": str(profile.get("primary_strategy") or ""),
            "low_liquidity": bool(profile.get("low_liquidity_market")),
            "use_case": str(profile.get("use_case") or ""),
        }
    return {}


def merge_intent_into_campaign(
    campaign: MutableMapping[str, Any],
    profile: IntentProfile,
) -> MutableMapping[str, Any]:
    """Attach intent_profile to campaign dict (in-memory). Fail-open."""
    try:
        campaign["intent_profile"] = profile.to_dict()
    except Exception:
        pass
    return campaign
