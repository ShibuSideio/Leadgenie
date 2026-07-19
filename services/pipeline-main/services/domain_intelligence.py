"""Domain intelligence helpers for campaign-aware query and gate behavior.

Infers a structured domain profile from sparse campaign + persona fields so
produce/dispatch can prune irrelevant sources, bias query portfolios, and
adapt gate strictness per vertical (real estate vs SaaS vs healthcare, etc.).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

from urllib.parse import urlparse

# Ensure ``shared.*`` resolves both in the monorepo (services/ on path) and in
# the pipeline container (services/shared copied to ./shared).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE_ROOT = os.path.dirname(_HERE)  # pipeline-main/
_SERVICES_ROOT = os.path.dirname(_SERVICE_ROOT)  # services/
for _path in (_SERVICE_ROOT, _SERVICES_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from shared.domain_constants import (  # type: ignore[import]
    DOMAIN_OVERRIDE_ALLOWED_KEYS,
    KNOWN_DOMAIN_FAMILIES,
    LIQUIDITY_LEVELS,
    allowed_domain_families_csv,
    is_valid_domain_family,
    normalize_domain_family,
)

try:
    from shared.education_profiles import (  # type: ignore[import]
        LEGACY_EDUCATION_QUERY_HINTS,
        LEGACY_EDUCATION_SOURCES,
        resolve_education_profile,
    )
except ImportError:  # pragma: no cover — monorepo path variants
    LEGACY_EDUCATION_QUERY_HINTS = (
        "site:reddit.com/r/teachers",
        "site:coursera.org",
        "site:linkedin.com",
    )
    LEGACY_EDUCATION_SOURCES = (
        "serper_discovery",
        "reddit",
        "consumer_forum",
        "rss_feed",
        "youtube",
    )

    def resolve_education_profile(*_a, **_k):  # type: ignore[misc]
        return {
            "education_sub_pattern": "general_education",
            "education_sub_pattern_confidence": 0.0,
            "education_matched_terms": [],
            "is_b2b_education": False,
            "language_pack": "general_education",
            "entity_terms": ["admission", "student", "parent", "course"],
            "preferred_query_hints": list(LEGACY_EDUCATION_QUERY_HINTS),
            "preferred_sources": list(LEGACY_EDUCATION_SOURCES),
            "platform_hosts": ["reddit.com", "quora.com", "youtube.com"],
            "resolve_error": True,
        }

try:
    from core.logging import get_logger  # type: ignore[import]
    _log = get_logger("pipeline.domain_intelligence")
except Exception:  # pragma: no cover
    _log = None  # type: ignore[assignment]

# Re-export for callers/tests that import from domain_intelligence.
__all_domain_constants__ = (
    "KNOWN_DOMAIN_FAMILIES",
    "is_valid_domain_family",
    "normalize_domain_family",
)

# ---------------------------------------------------------------------------
# Schema version — bump when profile shape or inference quality changes so
# produce/dispatch can re-infer stale system_domain_profile documents.
# domain-v3: education sub-pattern aware preferred platforms + entity language.
# ---------------------------------------------------------------------------
DOMAIN_PROFILE_VERSION = "domain-v3"

# Field weights: persona + pain/keywords are stronger ICP signals than name.
_FIELD_WEIGHTS: dict[str, float] = {
    "persona_name": 1.6,
    "persona_bio": 1.5,
    "persona_targeting_signals": 1.4,
    "pain_point": 1.3,
    "keywords": 1.25,
    "persona_keywords": 1.2,
    "effective_bio": 1.1,
    "bio": 1.0,
    "name": 0.85,
    "location": 0.35,
    "campaign_focus": 0.9,
}

# Domain keyword packs. Multi-word phrases score higher than single tokens.
# Each entry is (phrase, weight). Phrases are matched case-insensitively as
# substrings (after corpus normalization) so "real estate" and "crm software"
# both hit reliably.
_DOMAIN_TERM_PACKS: dict[str, tuple[tuple[str, float], ...]] = {
    "real_estate": (
        ("real estate", 3.0),
        ("property management", 2.5),
        ("property finder", 2.5),
        ("property broker", 2.5),
        ("real-estate", 3.0),
        ("commercial property", 2.2),
        ("residential property", 2.2),
        ("property agent", 2.2),
        ("property listing", 2.0),
        ("property portal", 2.0),
        ("for rent", 1.8),
        ("for sale", 1.5),
        ("villa", 1.6),
        ("apartment", 1.5),
        ("townhouse", 1.4),
        ("condo", 1.3),
        ("broker", 1.4),
        ("realtor", 1.8),
        ("landlord", 1.5),
        ("tenant", 1.2),
        ("lease", 1.1),
        ("rental", 1.4),
        ("rent", 1.0),
        ("mortgage", 1.3),
        ("listing", 1.0),
        ("bayut", 2.0),
        ("dubizzle", 1.5),
        ("zillow", 2.0),
        ("rightmove", 2.0),
        ("99acres", 2.0),
        ("magicbricks", 2.0),
        ("property", 1.2),
    ),
    "saas": (
        ("saas", 3.0),
        ("software as a service", 3.0),
        ("b2b software", 2.5),
        ("crm software", 2.3),
        ("subscription software", 2.2),
        ("workflow automation", 2.2),
        ("product-led growth", 2.0),
        ("api platform", 1.8),
        ("cloud software", 1.8),
        ("software platform", 1.8),
        ("crm", 1.6),
        ("erp", 1.5),
        ("automation", 1.2),
        ("workflow", 1.2),
        ("subscription", 1.2),
        ("churn", 1.5),
        ("mrr", 1.8),
        ("arr", 1.5),
        ("onboarding", 1.0),
        ("product demo", 1.4),
        ("g2.com", 1.8),
        ("capterra", 1.8),
        ("software", 1.0),
        ("platform", 0.8),
        ("api", 0.9),
        ("startup", 0.8),
    ),
    "manufacturing": (
        ("manufacturing", 3.0),
        ("factory", 2.2),
        ("industrial equipment", 2.3),
        ("production line", 2.2),
        ("oem", 1.8),
        ("procurement", 1.6),
        ("supplier", 1.4),
        ("fabrication", 1.8),
        ("machining", 1.8),
        ("cnc", 1.6),
        ("plant manager", 1.8),
        ("industrial", 1.3),
        ("machine tools", 2.0),
        ("equipment", 1.0),
        ("warehouse automation", 1.8),
        ("indiamart", 1.8),
        ("thomasnet", 1.8),
        ("assembly line", 1.8),
    ),
    "professional_services": (
        ("professional services", 3.0),
        ("consulting firm", 2.5),
        ("management consulting", 2.5),
        ("advisory services", 2.2),
        ("accounting firm", 2.2),
        ("audit firm", 2.0),
        ("law firm", 2.3),
        ("legal services", 2.2),
        ("agency services", 1.8),
        ("business consultant", 2.0),
        ("fractional cfo", 2.0),
        ("outsourced", 1.3),
        ("consultancy", 2.0),
        ("consultant", 1.5),
        ("advisory", 1.3),
        ("bookkeeping", 1.5),
        ("tax advisory", 2.0),
        ("white-collar", 1.4),
    ),
    "healthcare": (
        ("healthcare", 3.0),
        ("health care", 3.0),
        ("medical clinic", 2.5),
        ("hospital", 2.2),
        ("patient care", 2.2),
        ("telemedicine", 2.2),
        ("diagnostic", 1.8),
        ("physician", 1.8),
        ("dentist", 1.8),
        ("dental", 1.5),
        ("clinic", 1.6),
        ("doctor", 1.4),
        ("nurse", 1.2),
        ("medical", 1.3),
        ("pharma", 1.5),
        ("pharmacy", 1.4),
        ("ehr", 1.8),
        ("emr", 1.8),
        ("hipaa", 1.8),
        ("patient", 1.2),
        ("wellness", 1.0),
        ("therapy", 1.1),
    ),
    "education": (
        ("education", 2.5),
        ("edtech", 2.5),
        ("online course", 2.2),
        ("e-learning", 2.2),
        ("elearning", 2.2),
        ("university", 1.8),
        ("college", 1.6),
        ("school", 1.4),
        ("admission", 1.5),
        ("tuition", 1.6),
        ("curriculum", 1.5),
        ("student", 1.2),
        ("teacher", 1.2),
        ("tutoring", 1.8),
        ("learning management", 2.0),
        ("lms", 1.5),
        ("k-12", 1.8),
        ("higher education", 2.0),
        # Study-abroad / medical education / coaching signals
        ("study abroad", 2.8),
        ("overseas education", 2.6),
        ("mbbs", 2.6),
        ("medical education", 2.4),
        ("education consultant", 2.4),
        ("education counsellor", 2.4),
        ("coaching institute", 2.4),
        ("exam prep", 2.2),
        ("nursing", 1.4),
    ),
    "finance": (
        ("financial services", 2.8),
        ("fintech", 2.5),
        ("wealth management", 2.5),
        ("private equity", 2.2),
        ("investment banking", 2.2),
        ("insurance broker", 2.2),
        ("mortgage lender", 2.0),
        ("loan officer", 1.8),
        ("credit union", 1.8),
        ("asset management", 2.0),
        ("insurance", 1.6),
        ("mortgage", 1.4),
        ("banking", 1.5),
        ("finance", 1.4),
        ("loan", 1.2),
        ("credit", 1.1),
        ("underwriting", 1.6),
        ("compliance", 1.0),
        ("wealth", 1.2),
        ("brokerage", 1.4),
    ),
    "ecommerce": (
        ("e-commerce", 3.0),
        ("ecommerce", 3.0),
        ("online store", 2.3),
        ("shopify", 2.2),
        ("dtc brand", 2.2),
        ("d2c brand", 2.2),
        ("direct to consumer", 2.2),
        ("marketplace seller", 2.0),
        ("product catalog", 1.6),
        ("fulfillment", 1.4),
        ("dropshipping", 2.0),
        ("retail", 1.2),
        ("sku", 1.3),
        ("cart abandonment", 2.0),
        ("amazon fba", 2.0),
        ("online shop", 1.8),
    ),
    "hospitality": (
        ("hospitality", 2.8),
        ("hotel management", 2.5),
        ("restaurant", 1.8),
        ("tourism", 2.0),
        ("travel agency", 2.0),
        ("guest experience", 2.0),
        ("hotel", 1.5),
        ("resort", 1.6),
        ("booking", 1.1),
        ("airbnb", 1.8),
        ("fnb", 1.5),
        ("f&b", 1.5),
        ("catering", 1.5),
        ("short-term rental", 2.0),
    ),
    "logistics": (
        ("logistics", 2.8),
        ("supply chain", 2.8),
        ("freight forwarding", 2.5),
        ("last mile", 2.2),
        ("3pl", 2.2),
        ("warehousing", 2.0),
        ("fleet management", 2.0),
        ("shipping", 1.4),
        ("transportation", 1.5),
        ("courier", 1.5),
        ("customs clearance", 2.0),
        ("distribution", 1.2),
    ),
    "construction": (
        ("construction", 2.8),
        ("general contractor", 2.5),
        ("building materials", 2.2),
        ("civil engineering", 2.2),
        ("architecture firm", 2.0),
        ("fit-out", 1.8),
        ("fitout", 1.8),
        ("contractor", 1.5),
        ("renovation", 1.5),
        ("mep", 1.6),
        ("hvac contractor", 2.0),
        ("project site", 1.4),
        ("builder", 1.3),
    ),
    "hr_recruiting": (
        ("recruiting", 2.5),
        ("recruitment", 2.5),
        ("talent acquisition", 2.5),
        ("staffing agency", 2.5),
        ("hr software", 2.0),
        ("human resources", 2.0),
        ("headhunter", 2.0),
        ("executive search", 2.2),
        ("hiring", 1.2),
        ("job board", 1.6),
        ("ats", 1.5),
        ("workforce", 1.3),
    ),
    "marketing_agency": (
        ("marketing agency", 2.8),
        ("digital marketing", 2.3),
        ("performance marketing", 2.3),
        ("seo agency", 2.3),
        ("ppc agency", 2.2),
        ("branding agency", 2.2),
        ("content marketing", 1.8),
        ("lead generation agency", 2.2),
        ("ad agency", 2.0),
        ("growth marketing", 1.8),
        ("media buying", 1.8),
        ("social media marketing", 1.8),
        # Brand-strategy / narrative consultancies (e.g. Brand Narrative)
        ("brand narrative", 3.0),
        ("brand positioning", 2.8),
        ("brand identity", 2.6),
        ("brand architecture", 2.6),
        ("brand strategy", 2.5),
        ("brand storytelling", 2.4),
        ("marketing strategy", 2.3),
        ("retail marketing", 2.2),
        ("fmcg marketing", 2.4),
        ("fmcg", 1.8),
        ("differentiate", 1.6),
        ("brand differentiation", 2.4),
        ("go-to-market", 1.8),
        ("gtm strategy", 2.0),
        ("creative agency", 2.2),
        ("brand consultancy", 2.5),
        ("brand consulting", 2.5),
    ),
}

# Soft negative evidence: phrases that slightly reduce a domain score when
# present (disambiguation). Applied after positive scoring.
_DOMAIN_NEGATIVE_TERMS: dict[str, tuple[str, ...]] = {
    "real_estate": ("software lease", "lease software", "api lease", "saas lease"),
    "saas": ("medical software only as device",),  # rarely used; reserved
    "finance": ("finance department software",),  # weak; handled by saas pack
}

# Subreddits that commonly produce false positives for certain verticals.
_BLOCKED_SUBREDDITS_BY_DOMAIN: dict[str, set[str]] = {
    "real_estate": {"frugal", "buyitforlife", "personalfinance"},
    "saas": {"buyitforlife", "frugal"},
    "manufacturing": {"frugal", "buyitforlife"},
    "finance": {"wallstreetbets"},  # noisy retail chatter for B2B finance ICPs
    "healthcare": {"frugal"},
    "ecommerce": {"frugal"},
}

# Serper / query portfolio site: hints per domain family.
_PREFERRED_QUERY_HINTS: dict[str, tuple[str, ...]] = {
    "real_estate": (
        "site:propertyfinder",
        "site:bayut",
        "site:dubizzle",
        "site:olx",
        "site:reddit.com/r/oman",
        "site:reddit.com/r/expats",
    ),
    "saas": (
        "site:reddit.com/r/smallbusiness",
        "site:g2.com",
        "site:capterra.com",
        "site:trustpilot.com",
        "site:news.ycombinator.com",
    ),
    "manufacturing": (
        "site:indiamart.com",
        "site:thomasnet.com",
        "site:linkedin.com/posts",
    ),
    "professional_services": (
        "site:linkedin.com",
        "site:clutch.co",
        "site:reddit.com/r/consulting",
    ),
    "healthcare": (
        "site:reddit.com/r/healthcare",
        "site:practo.com",
        "site:google.com/maps",
    ),
    # Education family defaults are B2C student/parent surfaces.
    # Sub-pattern resolution (study_abroad / coaching / online_courses) may
    # replace these via resolve_education_profile(); keep general_education
    # here as the static fail-open baseline (NOT legacy /r/teachers+Coursera).
    "education": (
        "site:reddit.com",
        "site:quora.com",
        "site:youtube.com",
        "site:facebook.com/groups",
        "site:justdial.com",
    ),
    "finance": (
        "site:reddit.com/r/personalfinance",
        "site:linkedin.com",
        "site:trustpilot.com",
    ),
    "ecommerce": (
        "site:reddit.com/r/ecommerce",
        "site:trustpilot.com",
        "site:amazon.com",
    ),
    "hospitality": (
        "site:tripadvisor.com",
        "site:google.com/maps",
        "site:reddit.com",
    ),
    "logistics": (
        "site:linkedin.com",
        "site:reddit.com/r/logistics",
        "site:freightos.com",
    ),
    "construction": (
        "site:linkedin.com",
        "site:houzz.com",
        "site:reddit.com",
    ),
    "hr_recruiting": (
        "site:linkedin.com",
        "site:indeed.com",
        "site:glassdoor.com",
    ),
    "marketing_agency": (
        "site:reddit.com/r/agency",
        "site:clutch.co",
        "site:g2.com",
    ),
    "general_services": (
        "site:reddit.com",
        "site:google.com/maps",
        "site:trustpilot.com",
    ),
}

# Preferred signal-source plugin names (aligned with source_router / signal_sources).
_PREFERRED_SOURCES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "real_estate": (
        "classified_listings",
        "serper_discovery",
        "consumer_forum",
        "reddit",
        "google_reviews",
    ),
    "saas": (
        "reddit",
        "hackernews",
        "serper_discovery",
        "google_reviews",
        "rss_feed",
    ),
    "manufacturing": (
        "serper_discovery",
        "job_posts",
        "rss_feed",
        "reddit",
        "google_reviews",
    ),
    "professional_services": (
        "serper_discovery",
        "reddit",
        "google_reviews",
        "rss_feed",
        "job_posts",
    ),
    "healthcare": (
        "google_reviews",
        "serper_discovery",
        "consumer_forum",
        "reddit",
        "rss_feed",
    ),
    "education": (
        "serper_discovery",
        "reddit",
        "consumer_forum",
        "rss_feed",
        "youtube",
    ),
    "finance": (
        "serper_discovery",
        "reddit",
        "google_reviews",
        "rss_feed",
        "job_posts",
    ),
    "ecommerce": (
        "consumer_forum",
        "reddit",
        "serper_discovery",
        "youtube",
        "google_reviews",
    ),
    "hospitality": (
        "google_reviews",
        "consumer_forum",
        "serper_discovery",
        "reddit",
        "youtube",
    ),
    "logistics": (
        "serper_discovery",
        "job_posts",
        "reddit",
        "rss_feed",
        "google_reviews",
    ),
    "construction": (
        "serper_discovery",
        "classified_listings",
        "google_reviews",
        "job_posts",
        "reddit",
    ),
    "hr_recruiting": (
        "job_posts",
        "serper_discovery",
        "reddit",
        "rss_feed",
        "google_reviews",
    ),
    "marketing_agency": (
        "reddit",
        "serper_discovery",
        "google_reviews",
        "hackernews",
        "rss_feed",
    ),
    "general_services": (
        "serper_discovery",
        "reddit",
        "google_reviews",
        "consumer_forum",
    ),
}

# Baseline domain strictness: positive = stricter gates, negative = more lenient.
# Low-liquidity / high-noise verticals get a small negative bias so recovery
# modes can still promote sparse but valid leads.
_STRICTNESS_BIAS_BY_DOMAIN: dict[str, float] = {
    "real_estate": -0.15,
    "saas": 0.10,
    "manufacturing": -0.10,
    "professional_services": 0.05,
    "healthcare": 0.15,
    "education": 0.0,
    "finance": 0.20,
    "ecommerce": 0.0,
    "hospitality": -0.05,
    "logistics": 0.0,
    "construction": -0.05,
    "hr_recruiting": 0.05,
    "marketing_agency": 0.0,
    "general_services": -0.05,
}

# Default liquidity when geo does not force a downgrade.
_BASE_LIQUIDITY_BY_DOMAIN: dict[str, str] = {
    "real_estate": "medium",
    "saas": "high",
    "manufacturing": "medium",
    "professional_services": "medium",
    "healthcare": "medium",
    "education": "medium",
    "finance": "high",
    "ecommerce": "high",
    "hospitality": "medium",
    "logistics": "medium",
    "construction": "medium",
    "hr_recruiting": "high",
    "marketing_agency": "high",
    "general_services": "medium",
}

# Geo markers that typically mean thinner public OSINT volume.
_LOW_LIQUIDITY_MARKERS = frozenset({
    "oman",
    "muscat",
    "qatar",
    "doha",
    "bahrain",
    "manama",
    "kuwait",
    "nepal",
    "kathmandu",
    "bhutan",
    "thimphu",
    "maldives",
    "male",
    "brunei",
    "laos",
    "vientiane",
    "cambodia",
    "phnom penh",
    "myanmar",
    "yangon",
    "mongolia",
    "ulaanbaatar",
})

_MEDIUM_LIQUIDITY_MARKERS = frozenset({
    "uae",
    "dubai",
    "abu dhabi",
    "sharjah",
    "saudi",
    "riyadh",
    "jeddah",
    "ksa",
    "egypt",
    "cairo",
    "jordan",
    "amman",
    "morocco",
    "casablanca",
    "pakistan",
    "karachi",
    "lahore",
    "bangladesh",
    "dhaka",
    "sri lanka",
    "colombo",
    "kenya",
    "nairobi",
    "nigeria",
    "lagos",
    "vietnam",
    "hanoi",
    "ho chi minh",
})

_FALLBACK_PROFILE: dict[str, Any] = {
    "version": DOMAIN_PROFILE_VERSION,
    "domain_family": "general_services",
    "confidence": 0.25,
    "profile_confidence": "low",
    "thin_campaign": True,
    "input_richness": "low",
    "liquidity_level": "medium",
    "low_liquidity_market": False,
    "preferred_sources": list(_PREFERRED_SOURCES_BY_DOMAIN["general_services"]),
    "preferred_query_hints": list(_PREFERRED_QUERY_HINTS["general_services"]),
    "blocked_subreddits": [],
    "strictness_bias": 0.0,  # mild / neutral for empty fallbacks
    "soft_domain_adjustments": True,
    "notes": "fallback_profile: inference failed or empty campaign",
    "scores": {},
    "matched_terms": {},
}

# High-signal ICP fields used to judge thin vs rich campaigns.
_RICHNESS_CORE_FIELDS = (
    "persona_bio",
    "persona_name",
    "persona_targeting_signals",
    "pain_point",
    "keywords",
    "persona_keywords",
    "effective_bio",
    "bio",
    "campaign_focus",
)

# Extra light patterns for sparse name/keyword campaigns (not used when rich).
_THIN_SIGNAL_HINTS: dict[str, tuple[str, ...]] = {
    "real_estate": (
        "realty", "realtor", "housing", "homes", "listings", "rentals",
        "landlord", "tenant", "property", "estate",
    ),
    "saas": (
        "crm", "saas", "software", "platform", "automation", "startup",
        "b2b tech", "cloud app",
    ),
    "manufacturing": (
        "factory", "industrial", "oem", "cnc", "plant", "fabrication",
        "machinery", "procurement",
    ),
    "healthcare": (
        "clinic", "dental", "dentist", "hospital", "medical", "patient",
        "pharma", "therapy",
    ),
    "education": (
        "school", "tuition", "edtech", "course", "university", "training",
        "academy", "mbbs", "study abroad", "coaching", "admission",
    ),
    "finance": (
        "fintech", "lending", "insurance", "mortgage", "wealth", "banking",
        "credit",
    ),
    "ecommerce": (
        "shopify", "ecommerce", "e-commerce", "online store", "dtc", "d2c",
        "retail brand",
    ),
    "hospitality": (
        "hotel", "restaurant", "tourism", "hospitality", "resort", "fnb",
    ),
    "logistics": (
        "logistics", "freight", "shipping", "courier", "warehouse", "3pl",
    ),
    "construction": (
        "construction", "contractor", "builder", "fitout", "fit-out", "mep",
    ),
    "hr_recruiting": (
        "recruiting", "recruitment", "staffing", "hiring", "talent", "hr ",
    ),
    "marketing_agency": (
        "agency", "seo", "ppc", "digital marketing", "growth marketing",
        "media buying", "brand narrative", "brand positioning", "brand identity",
        "brand architecture", "brand strategy", "marketing strategy", "fmcg",
        "retail marketing", "creative agency", "brand consultancy",
    ),
    "professional_services": (
        "consulting", "consultancy", "advisory", "law firm", "accounting",
        "bookkeeping",
    ),
}


def _normalize_text(value: Any) -> str:
    """Coerce campaign field values into a single lowercase string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, (list, tuple, set)):
        parts: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, Mapping):
                # Persona targeting signals may be dicts with a text key.
                for key in ("text", "signal", "value", "label", "name"):
                    if item.get(key):
                        parts.append(str(item[key]))
                        break
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return " ".join(parts).strip().lower()
    if isinstance(value, Mapping):
        return " ".join(str(v) for v in value.values() if v is not None).strip().lower()
    return str(value).strip().lower()


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _extract_field_corpus(campaign: Mapping[str, Any]) -> dict[str, str]:
    """Pull and normalize the campaign fields used for domain inference.

    Priority fields for ICP fidelity:
      persona_name, persona_bio, persona_targeting_signals, pain_point,
      keywords, effective_bio, bio, name
    """
    fields = {
        "name": _normalize_text(campaign.get("name")),
        "bio": _normalize_text(campaign.get("bio")),
        "effective_bio": _normalize_text(campaign.get("effective_bio")),
        "pain_point": _normalize_text(campaign.get("pain_point")),
        "keywords": _normalize_text(campaign.get("keywords")),
        "persona_keywords": _normalize_text(campaign.get("persona_keywords")),
        "persona_name": _normalize_text(campaign.get("persona_name")),
        "persona_bio": _normalize_text(campaign.get("persona_bio")),
        "persona_targeting_signals": _normalize_text(
            campaign.get("persona_targeting_signals")
        ),
        "location": _normalize_text(campaign.get("location")),
        "campaign_focus": _normalize_text(campaign.get("campaign_focus")),
    }
    return {k: _collapse_ws(v) for k, v in fields.items() if v}


def _build_weighted_corpus(fields: Mapping[str, str]) -> list[tuple[str, float]]:
    """Return (text, weight) segments for scoring."""
    segments: list[tuple[str, float]] = []
    for field_name, text in fields.items():
        weight = float(_FIELD_WEIGHTS.get(field_name, 1.0))
        if text:
            segments.append((text, weight))
    return segments


def _phrase_in_text(phrase: str, text: str) -> bool:
    """True if phrase appears in text (substring for multi-word; word-ish for short tokens)."""
    if not phrase or not text:
        return False
    if " " in phrase or "-" in phrase or "." in phrase:
        return phrase in text
    # Single tokens: prefer word-boundary match to avoid "art" in "start".
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _score_domains(
    segments: Sequence[tuple[str, float]],
) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Score each domain family against weighted text segments.

    Returns:
        scores: domain_family -> cumulative weighted hit score
        matched_terms: domain_family -> list of matched phrases (deduped)
    """
    scores: dict[str, float] = {family: 0.0 for family in _DOMAIN_TERM_PACKS}
    matched: dict[str, list[str]] = {family: [] for family in _DOMAIN_TERM_PACKS}
    seen_match: dict[str, set[str]] = {family: set() for family in _DOMAIN_TERM_PACKS}

    for text, field_weight in segments:
        if not text:
            continue
        for family, terms in _DOMAIN_TERM_PACKS.items():
            for phrase, phrase_weight in terms:
                if not _phrase_in_text(phrase, text):
                    continue
                # Cap repeated hits of the same phrase across fields so one
                # repeated keyword cannot dominate the entire profile.
                if phrase in seen_match[family]:
                    scores[family] += 0.15 * field_weight * phrase_weight
                else:
                    scores[family] += field_weight * phrase_weight
                    seen_match[family].add(phrase)
                    matched[family].append(phrase)

        # Soft negatives
        for family, negatives in _DOMAIN_NEGATIVE_TERMS.items():
            for neg in negatives:
                if neg in text:
                    scores[family] = max(0.0, scores[family] - (1.2 * field_weight))

    return scores, matched


def _assess_input_richness(
    fields: Mapping[str, str],
    campaign: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Judge how much ICP signal the campaign provided (thin vs rich).

    Returns:
        {
          input_richness: "high"|"medium"|"low",
          thin_campaign: bool,
          core_field_count: int,
          core_char_count: int,
          has_persona: bool,
          total_char_count: int,
        }
    """
    core_chars = 0
    core_fields = 0
    for key in _RICHNESS_CORE_FIELDS:
        text = (fields.get(key) or "").strip()
        if not text:
            continue
        core_fields += 1
        core_chars += len(text)

    total_chars = sum(len(v) for v in fields.values())
    has_persona = bool(
        (fields.get("persona_bio") or fields.get("persona_name")
         or fields.get("persona_targeting_signals") or fields.get("persona_keywords"))
    )
    if isinstance(campaign, Mapping) and campaign.get("persona_id"):
        has_persona = has_persona or bool(str(campaign.get("persona_id") or "").strip())

    # Thresholds tuned so existing well-filled campaigns stay "high".
    if core_fields >= 3 and core_chars >= 120:
        richness = "high"
    elif core_fields >= 2 and core_chars >= 50:
        richness = "medium"
    elif core_fields >= 1 and core_chars >= 20:
        richness = "medium" if has_persona or core_chars >= 40 else "low"
    else:
        richness = "low"

    return {
        "input_richness": richness,
        "thin_campaign": richness == "low",
        "core_field_count": core_fields,
        "core_char_count": core_chars,
        "has_persona": has_persona,
        "total_char_count": total_chars,
    }


def _apply_thin_signal_boosts(
    scores: dict[str, float],
    matched: dict[str, list[str]],
    fields: Mapping[str, str],
    thin_campaign: bool,
) -> None:
    """In-place: add light industry hits from name/keywords when campaign is thin.

    Does not run on rich campaigns (preserves prior scoring behaviour).
    """
    if not thin_campaign:
        return
    # Prefer name + keywords only — avoid over-reading junk bios.
    blob = " ".join(
        filter(
            None,
            [
                fields.get("name", ""),
                fields.get("keywords", ""),
                fields.get("persona_keywords", ""),
                fields.get("campaign_focus", ""),
                fields.get("bio", "")[:80],  # short prefix only
            ],
        )
    )
    if not blob:
        return
    for family, hints in _THIN_SIGNAL_HINTS.items():
        for hint in hints:
            if not _phrase_in_text(hint, blob):
                continue
            # Modest boost so a single name token can surface a family without
            # dominating rich multi-field campaigns (which skip this path).
            scores[family] = scores.get(family, 0.0) + 1.1
            matched.setdefault(family, [])
            if hint not in matched[family]:
                matched[family].append(f"thin:{hint}")
            break  # one thin hit per family is enough


def _pick_domain_family(
    scores: Mapping[str, float],
    *,
    thin_campaign: bool = False,
) -> tuple[str, float, float]:
    """Select best domain family and return (family, best_score, runner_up_score)."""
    if not scores:
        return "general_services", 0.0, 0.0

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_family, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = best_score - runner_up

    # Thin campaigns: slightly lower bar so name/keyword industry signals
    # can still surface a family (tagged low profile_confidence later).
    min_score = 0.95 if thin_campaign else 1.5
    min_margin = 0.45 if thin_campaign else 0.75
    ambig_cap = 2.2 if thin_campaign else 3.0

    # Require a minimum absolute score; otherwise stay general.
    if best_score < min_score:
        return "general_services", best_score, runner_up

    # Ambiguous race: if top two are very close, stay general unless clear lead.
    if best_score < ambig_cap and margin < min_margin:
        return "general_services", best_score, runner_up

    return best_family, best_score, runner_up


def _confidence_from_scores(
    family: str,
    best_score: float,
    runner_up: float,
    matched_count: int,
    *,
    thin_campaign: bool = False,
) -> float:
    """Map score margin + hit diversity into a 0..1 confidence."""
    if family == "general_services":
        # Low but non-zero when we had some weak signal; pure empty stays low.
        if best_score <= 0:
            return 0.20
        return round(min(0.45, 0.22 + 0.04 * best_score), 3)

    # Base from absolute score (diminishing returns).
    base = min(0.55, 0.28 + 0.06 * best_score)
    # Diversity bonus: more distinct matched phrases → higher confidence.
    diversity = min(0.25, 0.04 * matched_count)
    # Margin bonus vs runner-up.
    margin = max(0.0, best_score - runner_up)
    margin_bonus = min(0.20, 0.03 * margin)

    confidence = min(0.98, base + diversity + margin_bonus)
    # Thin campaigns cannot claim high numeric confidence even with a hit.
    if thin_campaign:
        confidence = min(confidence, 0.58)
    return round(confidence, 3)


def _profile_confidence_tier(
    *,
    family: str,
    confidence: float,
    thin_campaign: bool,
    input_richness: str,
    matched_count: int,
    best_score: float,
) -> str:
    """Derive high/medium/low profile confidence tier for downstream damping."""
    if thin_campaign or input_richness == "low":
        # Sparse input: never high; upgrade to medium only with a clear match.
        if (
            family != "general_services"
            and confidence >= 0.42
            and matched_count >= 1
            and best_score >= 1.2
        ):
            return "medium"
        return "low"

    if family == "general_services":
        return "low" if confidence < 0.35 else "medium"

    if input_richness == "high" and confidence >= 0.65 and matched_count >= 2:
        return "high"
    if confidence >= 0.55 and matched_count >= 1:
        return "medium"
    if confidence >= 0.45 and input_richness == "medium":
        return "medium"
    return "low"


def _soften_for_low_profile_confidence(
    *,
    family: str,
    strictness: float,
    preferred_sources: list[str],
    preferred_hints: list[str],
    profile_confidence: str,
) -> tuple[float, list[str], list[str], bool]:
    """Apply safer defaults when profile_confidence is low.

    Returns (strictness, sources, hints, soft_domain_adjustments).
    """
    if profile_confidence == "high":
        return strictness, preferred_sources, preferred_hints, False

    soft = profile_confidence == "low"
    # Pull strictness toward neutral — keep a small fraction of domain bias.
    if profile_confidence == "medium":
        strictness = round(strictness * 0.65, 3)
    else:
        strictness = round(strictness * 0.35, 3)
        # Prefer slightly lenient over slightly strict when uncertain.
        if abs(strictness) < 0.03:
            strictness = -0.05 if family != "finance" else 0.05

    if soft:
        # Fewer platform injections; keep top sources only.
        preferred_sources = list(preferred_sources)[:3]
        preferred_hints = list(preferred_hints)[:2]
    elif profile_confidence == "medium":
        preferred_sources = list(preferred_sources)[:4]
        preferred_hints = list(preferred_hints)[:3]

    return strictness, preferred_sources, preferred_hints, soft


def _infer_liquidity_level(
    family: str,
    location_text: str,
    corpus_blob: str,
) -> tuple[str, bool]:
    """Return (liquidity_level, low_liquidity_market flag).

    Geo markers can downgrade domain base liquidity. Domain base already
    encodes typical public OSINT density (SaaS high, niche manufacturing medium).
    """
    base = _BASE_LIQUIDITY_BY_DOMAIN.get(family, "medium")
    haystack = f"{location_text} {corpus_blob}".lower()

    low_hit = any(marker in haystack for marker in _LOW_LIQUIDITY_MARKERS)
    medium_hit = any(marker in haystack for marker in _MEDIUM_LIQUIDITY_MARKERS)

    if low_hit:
        return "low", True
    if medium_hit and base == "high":
        return "medium", False
    if medium_hit and base == "medium":
        return "medium", False
    return base, False


def _strictness_for(
    family: str,
    liquidity_level: str,
    confidence: float,
) -> float:
    """Combine domain bias with liquidity/confidence adjustments."""
    bias = float(_STRICTNESS_BIAS_BY_DOMAIN.get(family, 0.0))
    if liquidity_level == "low":
        bias -= 0.15
    elif liquidity_level == "high" and confidence >= 0.7:
        bias += 0.05
    # Clamp to a sensible operational range for adaptive policy consumers.
    return round(max(-0.5, min(0.5, bias)), 3)


def _build_notes(
    family: str,
    confidence: float,
    liquidity_level: str,
    matched: Sequence[str],
    best_score: float,
    runner_up: float,
    field_names: Iterable[str],
    *,
    profile_confidence: str = "medium",
    thin_campaign: bool = False,
    input_richness: str = "medium",
    education_sub_pattern: str | None = None,
    language_pack: str | None = None,
) -> str:
    """Human-readable observations for logs and debugging."""
    parts: list[str] = []
    fields = sorted(set(field_names))
    parts.append(f"fields_used={','.join(fields) if fields else 'none'}")
    parts.append(f"input_richness={input_richness}")
    parts.append(f"profile_confidence={profile_confidence}")
    parts.append(f"score={best_score:.2f}")
    parts.append(f"runner_up={runner_up:.2f}")
    if matched:
        preview = ", ".join(list(matched)[:6])
        parts.append(f"matched=[{preview}]")
    if family == "general_services":
        parts.append("weak_or_ambiguous_domain_signal")
    if confidence < 0.4:
        parts.append("low_confidence")
    if thin_campaign:
        parts.append("thin_campaign")
    if profile_confidence == "low":
        parts.append("soft_domain_defaults")
    if liquidity_level == "low":
        parts.append("sparse_market_geo")
    if education_sub_pattern:
        parts.append(f"education_sub_pattern={education_sub_pattern}")
    if language_pack:
        parts.append(f"language_pack={language_pack}")
    return "; ".join(parts)


def _apply_education_vertical_profile(
    preferred_sources: list[str],
    preferred_hints: list[str],
    campaign: Mapping[str, Any] | None,
    *,
    force_legacy_on_error: bool = True,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Overlay education sub-pattern platforms onto family defaults.

    Fail-open: on any resolution failure, keep the caller-provided lists
    (or legacy education defaults if those lists are empty).
    """
    edu_meta: dict[str, Any] = {
        "education_sub_pattern": "general_education",
        "language_pack": "general_education",
        "is_b2b_education": False,
        "entity_terms": list(_PLATFORM_ENTITY_TERMS["education"]),
    }
    try:
        vector = None
        if isinstance(campaign, Mapping):
            vector = str(campaign.get("sourcing_vector") or "") or None
        edu = resolve_education_profile(campaign, sourcing_vector=vector)
        if edu.get("resolve_error") and force_legacy_on_error:
            # Module-level resolve failed — keep safer static education defaults
            # (already non-legacy in _PREFERRED_QUERY_HINTS["education"]).
            edu_meta["resolve_error"] = True
            return preferred_sources, preferred_hints, edu_meta

        edu_meta = {
            "education_sub_pattern": edu.get("education_sub_pattern") or "general_education",
            "education_sub_pattern_confidence": edu.get("education_sub_pattern_confidence"),
            "education_matched_terms": list(edu.get("education_matched_terms") or [])[:8],
            "is_b2b_education": bool(edu.get("is_b2b_education")),
            "language_pack": edu.get("language_pack") or "general_education",
            "entity_terms": list(edu.get("entity_terms") or _PLATFORM_ENTITY_TERMS["education"]),
            "platform_hosts": list(edu.get("platform_hosts") or []),
        }
        sources = list(edu.get("preferred_sources") or preferred_sources)
        hints = list(edu.get("preferred_query_hints") or preferred_hints)
        if not sources:
            sources = preferred_sources or list(LEGACY_EDUCATION_SOURCES)
        if not hints:
            hints = preferred_hints or list(
                _PREFERRED_QUERY_HINTS.get("education", LEGACY_EDUCATION_QUERY_HINTS)
            )

        if _log is not None:
            try:
                _log.info(
                    "domain_intelligence_education_sub_pattern",
                    education_sub_pattern=edu_meta["education_sub_pattern"],
                    language_pack=edu_meta["language_pack"],
                    is_b2b_education=edu_meta["is_b2b_education"],
                    sub_pattern_confidence=edu_meta.get("education_sub_pattern_confidence"),
                    matched_terms=edu_meta.get("education_matched_terms"),
                    preferred_hints_preview=hints[:4],
                    note="Education vertical resolved to sub-pattern platforms + language pack.",
                )
            except Exception:  # noqa: BLE001
                pass
        return sources, hints, edu_meta
    except Exception as exc:  # noqa: BLE001 — never break infer
        edu_meta["resolve_error"] = True
        edu_meta["error"] = f"{type(exc).__name__}: {exc}"
        if force_legacy_on_error and not preferred_hints:
            preferred_hints = list(
                _PREFERRED_QUERY_HINTS.get("education", LEGACY_EDUCATION_QUERY_HINTS)
            )
        if force_legacy_on_error and not preferred_sources:
            preferred_sources = list(LEGACY_EDUCATION_SOURCES)
        return preferred_sources, preferred_hints, edu_meta


def _safe_profile(**overrides: Any) -> dict[str, Any]:
    """Merge overrides onto the fallback profile (never mutate the constant)."""
    profile = dict(_FALLBACK_PROFILE)
    profile["preferred_sources"] = list(_FALLBACK_PROFILE["preferred_sources"])
    profile["preferred_query_hints"] = list(_FALLBACK_PROFILE["preferred_query_hints"])
    profile["blocked_subreddits"] = list(_FALLBACK_PROFILE["blocked_subreddits"])
    profile["scores"] = {}
    profile["matched_terms"] = {}
    profile.update(overrides)
    return profile


def infer_domain_profile(campaign: dict[str, Any] | None) -> dict[str, Any]:
    """Infer a rich domain profile from a campaign document.

    Analyzes multiple ICP fields (campaign + persona vault) with weighted
    keyword packs and returns a structured profile consumed by produce,
    dispatch, and adaptive policy.

    Thin/sparse campaigns (short bio, few keywords, no persona) get softer
    selection thresholds, light name/keyword industry hints, and a
    ``profile_confidence`` tier (high/medium/low). Low-tier profiles use
    milder ``strictness_bias`` and fewer preferred query hints so domain
    adjustments degrade gracefully.

    Well-filled campaigns keep the original pick thresholds and full
    domain bias (backward compatible).

    Args:
        campaign: Firestore campaign dict (or partial). May be None/empty.

    Returns:
        dict with at least:
          - version, domain_family, confidence (0–1)
          - profile_confidence ("high"|"medium"|"low")
          - thin_campaign, input_richness
          - liquidity_level, low_liquidity_market
          - preferred_sources, preferred_query_hints, blocked_subreddits
          - strictness_bias, soft_domain_adjustments
          - notes, scores, matched_terms
    """
    try:
        if not isinstance(campaign, Mapping):
            return _safe_profile(notes="fallback_profile: campaign is not a mapping")

        fields = _extract_field_corpus(campaign)
        if not fields:
            return _safe_profile(notes="fallback_profile: no usable campaign text fields")

        richness_meta = _assess_input_richness(fields, campaign)
        thin_campaign = bool(richness_meta["thin_campaign"])
        input_richness = str(richness_meta["input_richness"])

        segments = _build_weighted_corpus(fields)
        scores, matched_map = _score_domains(segments)
        # Sparse campaigns: allow light industry hits from name/keywords.
        _apply_thin_signal_boosts(scores, matched_map, fields, thin_campaign)

        family, best_score, runner_up = _pick_domain_family(
            scores, thin_campaign=thin_campaign
        )
        matched = matched_map.get(family, []) if family != "general_services" else []
        # If we fell back to general but some weak matches exist, keep top matches for notes.
        if family == "general_services" and scores:
            top_family = max(scores.items(), key=lambda kv: kv[1])[0]
            if scores.get(top_family, 0) > 0:
                matched = matched_map.get(top_family, [])[:4]

        confidence = _confidence_from_scores(
            family=family,
            best_score=best_score,
            runner_up=runner_up,
            matched_count=len(matched_map.get(family, [])),
            thin_campaign=thin_campaign,
        )

        location_text = fields.get("location", "")
        corpus_blob = " ".join(fields.values())
        liquidity_level, low_liquidity = _infer_liquidity_level(
            family, location_text, corpus_blob
        )
        strictness = _strictness_for(family, liquidity_level, confidence)

        profile_confidence = _profile_confidence_tier(
            family=family,
            confidence=confidence,
            thin_campaign=thin_campaign,
            input_richness=input_richness,
            matched_count=len(matched_map.get(family, [])),
            best_score=best_score,
        )

        preferred_sources = list(
            _PREFERRED_SOURCES_BY_DOMAIN.get(
                family, _PREFERRED_SOURCES_BY_DOMAIN["general_services"]
            )
        )
        preferred_hints = list(
            _PREFERRED_QUERY_HINTS.get(
                family, _PREFERRED_QUERY_HINTS["general_services"]
            )
        )

        # Education: replace family-static platforms with sub-pattern packs
        # (study_abroad / coaching / online_courses / general_education).
        education_meta: dict[str, Any] = {}
        if family == "education":
            preferred_sources, preferred_hints, education_meta = (
                _apply_education_vertical_profile(
                    preferred_sources,
                    preferred_hints,
                    campaign if isinstance(campaign, Mapping) else None,
                )
            )

        strictness, preferred_sources, preferred_hints, soft_adj = (
            _soften_for_low_profile_confidence(
                family=family,
                strictness=strictness,
                preferred_sources=preferred_sources,
                preferred_hints=preferred_hints,
                profile_confidence=profile_confidence,
            )
        )

        # Explainability: only top scores above zero, rounded.
        score_summary = {
            k: round(v, 3)
            for k, v in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            if v > 0
        }
        matched_summary = {
            k: v[:12]
            for k, v in matched_map.items()
            if v and (k == family or scores.get(k, 0) >= max(1.0, best_score * 0.5))
        }

        notes = _build_notes(
            family=family,
            confidence=confidence,
            liquidity_level=liquidity_level,
            matched=matched,
            best_score=best_score,
            runner_up=runner_up,
            field_names=fields.keys(),
            profile_confidence=profile_confidence,
            thin_campaign=thin_campaign,
            input_richness=input_richness,
            education_sub_pattern=education_meta.get("education_sub_pattern"),
            language_pack=education_meta.get("language_pack"),
        )

        profile_out: dict[str, Any] = {
            "version": DOMAIN_PROFILE_VERSION,
            "domain_family": family,
            "confidence": confidence,
            "profile_confidence": profile_confidence,
            "thin_campaign": thin_campaign,
            "input_richness": input_richness,
            "liquidity_level": liquidity_level,
            "low_liquidity_market": bool(low_liquidity),
            "preferred_sources": preferred_sources,
            "preferred_query_hints": preferred_hints,
            "blocked_subreddits": sorted(
                _BLOCKED_SUBREDDITS_BY_DOMAIN.get(family, set())
            ),
            "strictness_bias": strictness,
            "soft_domain_adjustments": soft_adj,
            "notes": notes,
            "scores": score_summary,
            "matched_terms": matched_summary,
            "richness_meta": {
                "core_field_count": richness_meta["core_field_count"],
                "core_char_count": richness_meta["core_char_count"],
                "has_persona": richness_meta["has_persona"],
            },
        }
        if family == "education" and education_meta:
            profile_out["education_sub_pattern"] = education_meta.get(
                "education_sub_pattern", "general_education"
            )
            profile_out["education_sub_pattern_confidence"] = education_meta.get(
                "education_sub_pattern_confidence"
            )
            profile_out["education_matched_terms"] = list(
                education_meta.get("education_matched_terms") or []
            )
            profile_out["is_b2b_education"] = bool(
                education_meta.get("is_b2b_education")
            )
            profile_out["language_pack"] = education_meta.get(
                "language_pack", "general_education"
            )
            profile_out["entity_terms"] = list(
                education_meta.get("entity_terms")
                or _PLATFORM_ENTITY_TERMS["education"]
            )
            if education_meta.get("platform_hosts"):
                profile_out["platform_hosts"] = list(education_meta["platform_hosts"])
        return profile_out
    except Exception as exc:  # noqa: BLE001 — must never break produce/dispatch
        return _safe_profile(
            notes=f"fallback_profile: inference_error ({type(exc).__name__}: {exc})",
        )


# Families that benefit from directory / platform site: injection after governance.
_PLATFORM_QUERY_FAMILIES = frozenset({
    "real_estate",
    "manufacturing",
    "construction",
    "professional_services",
    "healthcare",
    "hospitality",
    "education",
})

# Entity terms appended when building platform queries from preferred hints.
# Education uses sub-pattern packs from shared.education_profiles when available;
# the static entry here is the general_education B2C fail-open pack (no agent/broker).
_PLATFORM_ENTITY_TERMS: dict[str, tuple[str, ...]] = {
    "real_estate": ("agent", "broker", "listing", "contact"),
    "manufacturing": ("supplier", "manufacturer", "RFQ", "equipment"),
    "construction": ("contractor", "project", "tender", "contact"),
    "professional_services": ("consultant", "firm", "services", "contact"),
    "healthcare": ("clinic", "doctor", "patient", "review"),
    "hospitality": ("hotel", "restaurant", "guest", "review"),
    "saas": ("review", "alternative", "pricing", "integration"),
    "education": ("admission", "student", "parent", "course", "college", "school"),
    "general_services": ("review", "contact", "near me"),
}

# Tokens that count as "already has entity language" when expanding hints.
_ENTITY_LANGUAGE_MARKERS: frozenset[str] = frozenset({
    "agent", "broker", "supplier", "review", "listing", "contact", "rfq",
    "consultant", "counsellor", "counselor", "admission", "student", "parent",
    "tutor", "coaching", "tuition", "university", "college", "course",
    "enrollment", "instructor", "institute", "school", "looking for",
    "recommend", "manufacturer", "contractor",
})

# preferred_sources → default site: fragments when hints are sparse.
_SOURCE_SITE_SEEDS: dict[str, tuple[str, ...]] = {
    "classified_listings": (
        "site:propertyfinder",
        "site:bayut",
        "site:dubizzle",
        "site:olx",
    ),
    "google_reviews": (
        "site:google.com/maps",
        "site:trustpilot.com",
    ),
    "reddit": ("site:reddit.com",),
    "hackernews": ("site:news.ycombinator.com",),
    "job_posts": ("site:indeed.com", "site:linkedin.com/jobs"),
    "consumer_forum": ("site:reddit.com", "site:quora.com"),
    "serper_discovery": (),
    "rss_feed": (),
    "youtube": ("site:youtube.com",),
}


def _profile_has_query_signals(profile: Mapping[str, Any]) -> bool:
    """True when the profile carries any query-shaping fields."""
    if not profile:
        return False
    return bool(
        profile.get("domain_family")
        or profile.get("preferred_query_hints")
        or profile.get("preferred_sources")
        or profile.get("blocked_subreddits")
    )


def _blocked_subreddit_hit(query: str, blocked: set[str]) -> str | None:
    """Return the blocked subreddit name if *query* targets it, else None.

    Matches common Reddit patterns:
      - /r/{sub}
      - r/{sub} (token boundary)
      - site:reddit.com/r/{sub}
      - subreddit:{sub}
    """
    if not blocked or not query:
        return None
    lowered = query.lower()
    for sub in blocked:
        sub_l = str(sub).lower().strip().lstrip("r/").strip("/")
        if not sub_l:
            continue
        # /r/sub always (covers site:reddit.com/r/sub). Trailing boundary avoids
        # r/oman matching r/omanrealestate.
        if re.search(rf"/r/{re.escape(sub_l)}(?![a-z0-9])", lowered):
            return sub_l
        # Bare r/sub token (not preceded by word char or '/').
        if re.search(rf"(?<![a-z0-9/])r/{re.escape(sub_l)}(?![a-z0-9])", lowered):
            return sub_l
        if f"subreddit:{sub_l}" in lowered or f"subreddit={sub_l}" in lowered:
            return sub_l
    return None


def _location_token(location: str) -> str:
    if not location:
        return ""
    parts = [p.strip() for p in re.split(r"[,/|]+", location) if p and p.strip()]
    for part in parts:
        if part.lower() not in {"all", "global", "worldwide", "asia", "emea"}:
            return part
    return ""


def _primary_keyword(keywords: str) -> str:
    if not keywords:
        return ""
    first = keywords.split(",")[0].strip().strip('"').strip("'")
    # Keep short; avoid dumping entire bio-like keyword blobs into site: queries.
    if len(first) > 48:
        first = first[:48].rsplit(" ", 1)[0]
    return first


def _hint_covered(query: str, hint: str) -> bool:
    """True if query already expresses the preferred site:/platform hint."""
    q = (query or "").lower()
    h = (hint or "").lower().strip()
    if not q or not h:
        return False
    if h in q:
        return True
    # site:propertyfinder matches site:propertyfinder.ae etc.
    if h.startswith("site:"):
        host = h[5:].split("/")[0]
        if host and f"site:{host}" in q:
            return True
    return False


def _entity_terms_for_family(
    family: str,
    domain_profile: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Resolve entity language for platform-hint expansion.

    Education prefers profile-carried ``entity_terms`` / sub-pattern packs so
    we never fall through to real-estate agent/broker language.
    """
    if isinstance(domain_profile, Mapping):
        profile_terms = domain_profile.get("entity_terms") or domain_profile.get(
            "platform_entity_terms"
        )
        if isinstance(profile_terms, (list, tuple)) and profile_terms:
            cleaned = tuple(str(t).strip() for t in profile_terms if str(t).strip())
            if cleaned:
                return cleaned

    fam = (family or "").strip().lower()
    if fam == "education" and isinstance(domain_profile, Mapping):
        try:
            edu = resolve_education_profile(
                None,
                sourcing_vector=str(domain_profile.get("sourcing_vector") or "") or None,
                text=" ".join(
                    str(x)
                    for x in (
                        domain_profile.get("education_sub_pattern"),
                        domain_profile.get("notes"),
                    )
                    if x
                ),
            )
            # Prefer explicit sub-pattern on profile over re-detect.
            sub = str(domain_profile.get("education_sub_pattern") or edu.get("education_sub_pattern") or "")
            is_b2b = bool(domain_profile.get("is_b2b_education"))
            from shared.education_profiles import education_entity_terms  # type: ignore[import]
            return tuple(education_entity_terms(sub, is_b2b=is_b2b))
        except Exception:  # noqa: BLE001
            return _PLATFORM_ENTITY_TERMS["education"]

    return _PLATFORM_ENTITY_TERMS.get(fam) or _PLATFORM_ENTITY_TERMS["general_services"]


def _hint_has_entity_language(hint: str) -> bool:
    lower = (hint or "").lower()
    if not lower:
        return False
    for tok in _ENTITY_LANGUAGE_MARKERS:
        if tok in lower:
            return True
    return False


def _build_query_from_hint(
    hint: str,
    *,
    family: str,
    location: str,
    keywords: str,
    domain_profile: Mapping[str, Any] | None = None,
) -> str:
    """Expand a preferred_query_hint into an executable Serper query string."""
    hint = (hint or "").strip()
    if not hint:
        return ""
    entities = _entity_terms_for_family(family, domain_profile)
    loc = _location_token(location)
    kw = _primary_keyword(keywords)

    # Hint may already be a full-ish query; only append light context.
    base = hint
    if not _hint_has_entity_language(hint):
        base = f"{hint} {entities[0]} {entities[1] if len(entities) > 1 else 'contact'}"
    if loc and loc.lower() not in base.lower():
        base = f'{base} "{loc}"'
    if kw and kw.lower() not in base.lower() and len(kw) > 2:
        base = f'{base} "{kw}"'
    base = f"{base} -wiki -jobs -careers"
    return re.sub(r"\s{2,}", " ", base).strip()


def _collect_injection_hints(profile: Mapping[str, Any], family: str) -> list[str]:
    """Merge preferred_query_hints with source-derived site seeds (deduped).

    Explicit profile hints always win. Source seeds fill gaps. Family default
    platforms are only added when the profile is sparse or the market is
    low-liquidity (broader discovery needed).
    """
    hints: list[str] = []
    seen: set[str] = set()

    for h in profile.get("preferred_query_hints") or []:
        text = str(h).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        hints.append(text)

    explicit_count = len(hints)

    for source in profile.get("preferred_sources") or []:
        src = str(source).strip().lower()
        for seed in _SOURCE_SITE_SEEDS.get(src, ()):
            key = seed.lower()
            if key in seen:
                continue
            if len(hints) >= 6:
                break
            seen.add(key)
            hints.append(seed)

    low_liq = bool(profile.get("low_liquidity_market")) or str(
        profile.get("liquidity_level") or ""
    ).lower() == "low"

    # Family defaults only when: no explicit hints, or low-liquidity with
    # still-thin coverage. Avoids over-injecting on every real_estate run.
    needs_family_defaults = family in _PLATFORM_QUERY_FAMILIES and (
        explicit_count == 0 or (low_liq and explicit_count < 2)
    )
    if needs_family_defaults:
        for seed in _PREFERRED_QUERY_HINTS.get(family, ())[:4]:
            key = seed.lower()
            if key not in seen:
                seen.add(key)
                hints.append(seed)
    return hints


def apply_domain_query_profile(
    queries: list[str],
    domain_profile: dict[str, Any] | None,
    *,
    location: str = "",
    keywords: str = "",
    max_inject: int = 3,
) -> dict[str, Any]:
    """Shape a governed query portfolio with domain profile signals.

    Intended to run **after** ``govern_query_portfolio`` and **before** Serper.

    Actions (only when a usable domain profile is present):
      1. Drop queries targeting ``blocked_subreddits`` (broader Reddit patterns).
      2. Boost / reorder queries that already match ``preferred_query_hints``
         or preferred platform hosts.
      3. Inject missing preferred site: / platform queries (capped).
      4. For platform-heavy families (real_estate, manufacturing, …), ensure
         at least one directory-oriented site: query is present.

    When *domain_profile* is missing/empty, returns the input queries unchanged.

    Returns:
        {
          "queries": list[str],
          "dropped": int,
          "injected": int,
          "boosted": int,
          "reordered": bool,
          "domain_family": str | None,
        }
    """
    empty_meta = {
        "dropped": 0,
        "injected": 0,
        "boosted": 0,
        "reordered": False,
        "domain_family": None,
    }
    try:
        if not isinstance(domain_profile, Mapping) or not _profile_has_query_signals(domain_profile):
            return {"queries": list(queries or []), **empty_meta}

        family = str(domain_profile.get("domain_family") or "general_services").strip().lower()
        profile_confidence = str(
            domain_profile.get("profile_confidence") or ""
        ).strip().lower()
        thin_campaign = bool(domain_profile.get("thin_campaign"))
        soft_adj = bool(domain_profile.get("soft_domain_adjustments"))
        # Low-confidence / thin profiles: damp injection aggressiveness.
        if profile_confidence == "low" or soft_adj or thin_campaign:
            max_inject = min(int(max_inject), 1)
        elif profile_confidence == "medium":
            max_inject = min(int(max_inject), 2)

        blocked = {
            str(s).lower().strip().lstrip("r/").strip("/")
            for s in (domain_profile.get("blocked_subreddits") or [])
            if str(s).strip()
        }
        hints = _collect_injection_hints(domain_profile, family)
        # Low confidence: only use explicit preferred_query_hints on the profile,
        # not family-default platform flood from preferred_sources seeds.
        if profile_confidence == "low" or soft_adj:
            explicit = [
                str(h).strip()
                for h in (domain_profile.get("preferred_query_hints") or [])
                if str(h).strip()
            ]
            if explicit:
                hints = explicit[:2]
            else:
                hints = hints[:1]
        hint_keys = [h.lower() for h in hints]

        if not queries and not hints:
            return {"queries": [], **empty_meta, "domain_family": family}

        # ── 1. Drop blocked-subreddit noise ──────────────────────────────
        kept: list[str] = []
        dropped = 0
        seen: set[str] = set()
        for query in queries or []:
            q = (query or "").strip()
            if not q:
                continue
            hit = _blocked_subreddit_hit(q, blocked)
            if hit:
                dropped += 1
                continue
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            kept.append(q)

        # ── 2. Score & boost preferred matches ───────────────────────────
        def _pref_score(q: str) -> int:
            score = 0
            for h in hint_keys:
                if _hint_covered(q, h):
                    score += 3
            # Light boost for any site: query when platform family.
            if family in _PLATFORM_QUERY_FAMILIES and re.search(
                r"(?<![-\w])site:", q, flags=re.IGNORECASE
            ):
                score += 1
            return score

        scored = [(_pref_score(q), idx, q) for idx, q in enumerate(kept)]
        boosted = sum(1 for s, _, _ in scored if s > 0)
        reordered = False
        if boosted:
            scored.sort(key=lambda t: (-t[0], t[1]))
            new_kept = [q for _, _, q in scored]
            if new_kept != kept:
                reordered = True
            kept = new_kept
            seen = {q.lower() for q in kept}

        # ── 3. Inject missing preferred / platform queries ───────────────
        injected = 0
        inject_budget = max(0, int(max_inject))
        platform_count = sum(
            1
            for q in kept
            if re.search(r"(?<![-\w])site:", q or "", flags=re.IGNORECASE)
        )
        # Platform families: aim for at least 2 site: queries when liquidity
        # is low or family is directory-heavy.
        low_liq = bool(domain_profile.get("low_liquidity_market")) or str(
            domain_profile.get("liquidity_level") or ""
        ).lower() == "low"
        min_platform = 0
        if family in _PLATFORM_QUERY_FAMILIES and profile_confidence != "low" and not soft_adj:
            min_platform = 2 if low_liq else 1
        elif family in _PLATFORM_QUERY_FAMILIES and (profile_confidence == "low" or soft_adj):
            # Thin/low-confidence: at most aim for a single platform query.
            min_platform = 1 if low_liq else 0

        def _need_more_coverage() -> bool:
            if inject_budget <= 0:
                return False
            if not hints:
                return False
            missing = [
                h for h in hints
                if not any(_hint_covered(q, h) for q in kept)
            ]
            if missing:
                return True
            if platform_count < min_platform:
                return True
            return False

        if _need_more_coverage():
            for hint in hints:
                if injected >= inject_budget:
                    break
                if any(_hint_covered(q, hint) for q in kept):
                    continue
                # Prefer re-promoting an original dropped-by-dedup query first.
                resurrected = None
                for query in queries or []:
                    if _hint_covered(query, hint) and not _blocked_subreddit_hit(
                        query, blocked
                    ):
                        resurrected = (query or "").strip()
                        break
                candidate = resurrected or _build_query_from_hint(
                    hint,
                    family=family,
                    location=location,
                    keywords=keywords,
                    domain_profile=domain_profile,
                )
                if not candidate:
                    continue
                key = candidate.lower()
                if key in seen:
                    continue
                if _blocked_subreddit_hit(candidate, blocked):
                    continue
                kept.insert(injected, candidate)  # front-load domain injections
                seen.add(key)
                injected += 1
                if re.search(r"(?<![-\w])site:", candidate, flags=re.IGNORECASE):
                    platform_count += 1

        return {
            "queries": kept,
            "dropped": dropped,
            "injected": injected,
            "boosted": boosted,
            "reordered": reordered,
            "domain_family": family,
            "profile_confidence": profile_confidence or None,
            "thin_campaign": thin_campaign,
        }
    except Exception:
        # Fail open: never drop the producer's query set on profile errors.
        return {"queries": list(queries or []), **empty_meta}


def filter_tiered_urls_by_domain(
    tiered: dict[str, list[str]],
    domain_profile: dict[str, Any],
) -> dict[str, Any]:
    """Drop High/Medium tier URLs that land on domain-blocked subreddits.

    Low-tier URLs are left intact (already deprioritized). Fail-open on errors.
    """
    try:
        blocked = {
            str(s).lower()
            for s in (domain_profile.get("blocked_subreddits") or [])
            if str(s).strip()
        }
        if not blocked:
            return {"tiered": tiered, "dropped": 0}

        dropped = 0
        filtered: dict[str, list[str]] = {
            "High": [],
            "Medium": [],
            "Low": list(tiered.get("Low", []) or []),
        }
        for tier in ("High", "Medium"):
            for url in tiered.get(tier, []) or []:
                try:
                    parsed = urlparse(url)
                    parts = [p.lower() for p in parsed.path.split("/") if p]
                    subreddit = (
                        parts[1]
                        if len(parts) >= 2 and parts[0] == "r"
                        else ""
                    )
                    if subreddit and subreddit in blocked:
                        dropped += 1
                        continue
                except Exception:
                    pass
                filtered[tier].append(url)
        return {"tiered": filtered, "dropped": dropped}
    except Exception:
        return {"tiered": tiered, "dropped": 0}


def domain_profile_is_current(profile: Any) -> bool:
    """True if *profile* is a dict at the current DOMAIN_PROFILE_VERSION."""
    return (
        isinstance(profile, dict)
        and str(profile.get("version") or "") == DOMAIN_PROFILE_VERSION
        and bool(profile.get("domain_family"))
    )


# ---------------------------------------------------------------------------
# Manual domain override (campaign.domain_override)
# Family allowlist + helpers live in shared.domain_constants (SSOT).
# ---------------------------------------------------------------------------

# Back-compat alias used by older call sites / validation messages.
_LIQUIDITY_LEVELS = LIQUIDITY_LEVELS
_OVERRIDE_ALLOWED_KEYS = DOMAIN_OVERRIDE_ALLOWED_KEYS


def _is_cleared_override(raw: Any) -> bool:
    """True when override is absent or explicitly cleared by the caller."""
    if raw is None or raw is False:
        return True
    if isinstance(raw, str) and not raw.strip():
        return True
    if isinstance(raw, Mapping) and len(raw) == 0:
        return True
    return False


def _normalize_family_name(value: Any) -> str | None:
    """Delegate to shared.normalize_domain_family (kept for local call sites)."""
    return normalize_domain_family(value)


def validate_domain_override(
    raw: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and lightly normalize a campaign ``domain_override`` payload.

    Accepts:
      - ``None`` / ``""`` / ``{}`` / ``False`` → clear override ``(None, None)``
      - string family name, e.g. ``"real_estate"``
      - partial or full profile dict with at least ``domain_family``

    Returns:
        ``(normalized_override_or_None, error_message_or_None)``
        On clear: ``(None, None)``.
        On error: ``(None, "reason")``.
        On success: ``({...user override fields...}, None)``.
    """
    if _is_cleared_override(raw):
        return None, None

    # Shorthand: bare family string
    if isinstance(raw, str):
        family = normalize_domain_family(raw)
        if not family:
            return None, (
                f"Unknown domain_family '{raw}'. "
                f"Allowed: {allowed_domain_families_csv()}"
            )
        return {"domain_family": family}, None

    if not isinstance(raw, Mapping):
        return None, "domain_override must be an object, a family string, or null to clear"

    # Reject unknown top-level keys (keep API strict, prevent junk fields).
    unknown = [str(k) for k in raw.keys() if str(k) not in DOMAIN_OVERRIDE_ALLOWED_KEYS]
    if unknown:
        return None, f"Unsupported domain_override keys: {', '.join(sorted(unknown))}"

    family = normalize_domain_family(raw.get("domain_family"))
    if not family:
        return None, (
            "domain_override.domain_family is required and must be one of: "
            + allowed_domain_families_csv()
        )

    out: dict[str, Any] = {"domain_family": family}

    if "confidence" in raw and raw.get("confidence") is not None:
        try:
            conf = float(raw["confidence"])
            if conf != conf:  # NaN
                return None, "domain_override.confidence must be a number between 0 and 1"
            out["confidence"] = round(max(0.0, min(1.0, conf)), 3)
        except (TypeError, ValueError):
            return None, "domain_override.confidence must be a number between 0 and 1"

    if "liquidity_level" in raw and raw.get("liquidity_level") is not None:
        liq = str(raw.get("liquidity_level") or "").strip().lower()
        if liq not in LIQUIDITY_LEVELS:
            return None, "domain_override.liquidity_level must be high, medium, or low"
        out["liquidity_level"] = liq

    if "low_liquidity_market" in raw and raw.get("low_liquidity_market") is not None:
        out["low_liquidity_market"] = bool(raw.get("low_liquidity_market"))

    if "strictness_bias" in raw and raw.get("strictness_bias") is not None:
        try:
            bias = float(raw["strictness_bias"])
            if bias != bias:
                return None, "domain_override.strictness_bias must be a number in [-0.5, 0.5]"
            out["strictness_bias"] = round(max(-0.5, min(0.5, bias)), 3)
        except (TypeError, ValueError):
            return None, "domain_override.strictness_bias must be a number in [-0.5, 0.5]"

    for list_key in ("preferred_sources", "preferred_query_hints", "blocked_subreddits"):
        if list_key not in raw or raw.get(list_key) is None:
            continue
        val = raw.get(list_key)
        if isinstance(val, str):
            items = [val.strip()] if val.strip() else []
        elif isinstance(val, (list, tuple, set)):
            items = [str(x).strip() for x in val if str(x).strip()]
        else:
            return None, f"domain_override.{list_key} must be a list of strings"
        # Cap list sizes to keep Firestore docs lean.
        out[list_key] = items[:20]

    if "notes" in raw and raw.get("notes") is not None:
        notes = str(raw.get("notes") or "").strip()
        if notes:
            out["notes"] = notes[:500]

    return out, None


def expand_domain_override(
    override: Mapping[str, Any],
    campaign: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Expand a validated partial override into a full system_domain_profile.

    Family defaults fill any fields not explicitly set on the override.
    Geo from the campaign can still mark low-liquidity when not overridden.
    """
    family = str(override.get("domain_family") or "general_services")
    if not is_valid_domain_family(family):
        family = "general_services"

    # Liquidity: explicit override wins; else derive from campaign location + family base.
    if override.get("liquidity_level") in _LIQUIDITY_LEVELS:
        liquidity_level = str(override["liquidity_level"])
        low_liquidity = bool(
            override["low_liquidity_market"]
            if "low_liquidity_market" in override
            else liquidity_level == "low"
        )
    else:
        location_text = ""
        if isinstance(campaign, Mapping):
            location_text = _normalize_text(campaign.get("location"))
        liquidity_level, low_liquidity = _infer_liquidity_level(
            family, location_text, location_text
        )
        if "low_liquidity_market" in override:
            low_liquidity = bool(override.get("low_liquidity_market"))
            if low_liquidity:
                liquidity_level = "low"

    if "strictness_bias" in override and override.get("strictness_bias") is not None:
        try:
            strictness = max(-0.5, min(0.5, float(override["strictness_bias"])))
        except (TypeError, ValueError):
            strictness = _strictness_for(family, liquidity_level, 1.0)
    else:
        strictness = _strictness_for(family, liquidity_level, 1.0)

    if "confidence" in override and override.get("confidence") is not None:
        try:
            confidence = max(0.0, min(1.0, float(override["confidence"])))
        except (TypeError, ValueError):
            confidence = 1.0
    else:
        confidence = 1.0  # manual overrides are authoritative

    preferred_sources = override.get("preferred_sources")
    if not preferred_sources:
        preferred_sources = list(
            _PREFERRED_SOURCES_BY_DOMAIN.get(
                family, _PREFERRED_SOURCES_BY_DOMAIN["general_services"]
            )
        )

    preferred_hints = override.get("preferred_query_hints")
    if not preferred_hints:
        preferred_hints = list(
            _PREFERRED_QUERY_HINTS.get(family, _PREFERRED_QUERY_HINTS["general_services"])
        )

    education_meta: dict[str, Any] = {}
    # When override only pins the family (no explicit platforms), enrich
    # education with sub-pattern packs from campaign text. Explicit override
    # platforms are left untouched.
    if family == "education" and not override.get("preferred_query_hints"):
        preferred_sources, preferred_hints, education_meta = (
            _apply_education_vertical_profile(
                list(preferred_sources),
                list(preferred_hints),
                campaign if isinstance(campaign, Mapping) else None,
            )
        )

    blocked = override.get("blocked_subreddits")
    if blocked is None:
        blocked = sorted(_BLOCKED_SUBREDDITS_BY_DOMAIN.get(family, set()))

    user_notes = str(override.get("notes") or "").strip()
    notes = "manual_domain_override"
    if user_notes:
        notes = f"manual_domain_override; {user_notes}"
    if education_meta.get("education_sub_pattern"):
        notes = (
            f"{notes}; education_sub_pattern={education_meta['education_sub_pattern']}; "
            f"language_pack={education_meta.get('language_pack') or 'general_education'}"
        )

    profile_out: dict[str, Any] = {
        "version": DOMAIN_PROFILE_VERSION,
        "domain_family": family,
        "confidence": round(float(confidence), 3),
        "profile_confidence": "high",  # manual overrides are authoritative
        "thin_campaign": False,
        "input_richness": "high",
        "liquidity_level": liquidity_level,
        "low_liquidity_market": bool(low_liquidity),
        "preferred_sources": list(preferred_sources)[:20],
        "preferred_query_hints": list(preferred_hints)[:20],
        "blocked_subreddits": list(blocked)[:20],
        "strictness_bias": round(float(strictness), 3),
        "soft_domain_adjustments": False,
        "notes": notes,
        "scores": {},
        "matched_terms": {},
        "override_active": True,
        "override_source": "domain_override",
    }
    if family == "education" and education_meta:
        profile_out["education_sub_pattern"] = education_meta.get(
            "education_sub_pattern", "general_education"
        )
        profile_out["education_sub_pattern_confidence"] = education_meta.get(
            "education_sub_pattern_confidence"
        )
        profile_out["education_matched_terms"] = list(
            education_meta.get("education_matched_terms") or []
        )
        profile_out["is_b2b_education"] = bool(education_meta.get("is_b2b_education"))
        profile_out["language_pack"] = education_meta.get(
            "language_pack", "general_education"
        )
        profile_out["entity_terms"] = list(
            education_meta.get("entity_terms") or _PLATFORM_ENTITY_TERMS["education"]
        )
        if education_meta.get("platform_hosts"):
            profile_out["platform_hosts"] = list(education_meta["platform_hosts"])
    return profile_out


def resolve_campaign_domain_profile(
    campaign: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve the effective domain profile for a campaign.

    Precedence:
      1. Valid ``campaign.domain_override`` → expanded full profile (override wins)
      2. Current cached ``system_domain_profile`` (auto-inferred, not override-stale)
      3. Fresh ``infer_domain_profile(campaign)``

    Clearing ``domain_override`` (missing/null/empty) falls back to auto-detect.
    If the cached profile was produced by a previous override, it is discarded
    and re-inferred so removing the override restores normal behaviour.

    Returns:
        ``(profile, meta)`` where meta includes:
          - source: override | cached | inferred | inferred_invalid_override
          - override_active: bool
          - should_persist: bool (caller should write system_domain_profile)
          - error: optional validation error when override was invalid
    """
    camp = campaign if isinstance(campaign, Mapping) else {}
    override_raw = camp.get("domain_override")
    cached = camp.get("system_domain_profile")
    cached_ok = domain_profile_is_current(cached)
    cached_was_override = bool(
        isinstance(cached, dict) and cached.get("override_active")
    )

    # ── Manual override path ────────────────────────────────────────────
    if not _is_cleared_override(override_raw):
        normalized, err = validate_domain_override(override_raw)
        if err or not normalized:
            # Invalid override must not break the pipeline — fall back.
            profile = infer_domain_profile(dict(camp) if camp else None)
            profile["override_active"] = False
            return profile, {
                "source": "inferred_invalid_override",
                "override_active": False,
                "should_persist": True,
                "error": err or "invalid domain_override",
            }

        profile = expand_domain_override(normalized, campaign=camp)
        # Skip rewrite when cached already reflects the same override snapshot.
        if cached_ok and cached_was_override:
            same = (
                str(cached.get("domain_family")) == str(profile.get("domain_family"))
                and str(cached.get("liquidity_level") or "")
                == str(profile.get("liquidity_level") or "")
                and round(float(cached.get("strictness_bias") or 0), 3)
                == round(float(profile.get("strictness_bias") or 0), 3)
                and list(cached.get("preferred_sources") or [])
                == list(profile.get("preferred_sources") or [])
                and list(cached.get("blocked_subreddits") or [])
                == list(profile.get("blocked_subreddits") or [])
            )
            if same:
                return dict(cached), {
                    "source": "override",
                    "override_active": True,
                    "should_persist": False,
                }
        return profile, {
            "source": "override",
            "override_active": True,
            "should_persist": True,
        }

    # ── No override: auto path ──────────────────────────────────────────
    # Stale override-backed cache must be replaced after clear.
    if cached_ok and not cached_was_override:
        cleaned = dict(cached)
        cleaned["override_active"] = False
        return cleaned, {
            "source": "cached",
            "override_active": False,
            "should_persist": False,
        }

    profile = infer_domain_profile(dict(camp) if camp else None)
    profile["override_active"] = False
    return profile, {
        "source": "inferred",
        "override_active": False,
        "should_persist": True,
        "reason": "override_cleared" if cached_was_override else "missing_or_stale_cache",
    }


def build_domain_impact_summary(
    domain_profile: Mapping[str, Any] | None = None,
    *,
    policy: Mapping[str, Any] | None = None,
    query_stats: Mapping[str, Any] | None = None,
    prefilter_domain_softening: bool | None = None,
    domain_tier_dropped: int | None = None,
    leads_promoted: int | None = None,
    leads_scored_out: int | None = None,
    cycle: str = "dispatch",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact, structured domain-impact summary for a produce/dispatch cycle.

    Designed for end-of-cycle logs (``produce_domain_impact_summary`` /
    ``dispatch_domain_impact_summary``) and optional persistence on
    ``scored_out`` lead docs. Missing inputs become ``None`` / ``0`` so the
    schema stays stable across stages.

    Args:
        domain_profile: Campaign ``system_domain_profile`` (may be None).
        policy: Adaptive policy dict from ``build_dispatch_policy`` (dispatch).
        query_stats: Result stats from ``apply_domain_query_profile`` (produce).
        prefilter_domain_softening: Whether Gemini pre-filter directory softening
            was active for this cycle (dispatch).
        domain_tier_dropped: URLs dropped by domain tier/subreddit filter.
        leads_promoted: Leads that passed the confidence gate (dispatch).
        leads_scored_out: Leads rejected at the confidence gate (dispatch).
        cycle: ``"produce"`` or ``"dispatch"`` (or other label).
        extra: Optional flat fields merged last (non-breaking extension point).

    Returns:
        Flat JSON-serializable dict with stable keys.
    """
    profile = domain_profile if isinstance(domain_profile, Mapping) else {}
    pol = policy if isinstance(policy, Mapping) else {}
    qstats = query_stats if isinstance(query_stats, Mapping) else {}

    # Prefer policy echo fields when present (already clamped / applied).
    strictness_bias = pol.get("domain_strictness_bias")
    if strictness_bias is None:
        strictness_bias = profile.get("strictness_bias")
    try:
        strictness_bias_f = (
            round(float(strictness_bias), 3) if strictness_bias is not None else None
        )
    except (TypeError, ValueError):
        strictness_bias_f = None

    threshold_adj = pol.get("threshold_adjustment")
    try:
        threshold_adj_f = (
            round(float(threshold_adj), 3) if threshold_adj is not None else None
        )
    except (TypeError, ValueError):
        threshold_adj_f = None

    domain_delta = pol.get("domain_threshold_delta")
    try:
        domain_delta_f = (
            round(float(domain_delta), 3) if domain_delta is not None else None
        )
    except (TypeError, ValueError):
        domain_delta_f = None

    confidence = profile.get("confidence")
    try:
        confidence_f = round(float(confidence), 3) if confidence is not None else None
    except (TypeError, ValueError):
        confidence_f = None

    def _int_or_none(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    queries_dropped = _int_or_none(qstats.get("dropped"))
    queries_injected = _int_or_none(qstats.get("injected"))
    queries_boosted = _int_or_none(qstats.get("boosted"))
    queries_reordered = qstats.get("reordered")
    if queries_reordered is not None:
        queries_reordered = bool(queries_reordered)

    domain_family = (
        pol.get("domain_family")
        or profile.get("domain_family")
        or qstats.get("domain_family")
        or None
    )
    if domain_family is not None:
        domain_family = str(domain_family)

    liquidity_level = pol.get("liquidity_level") or profile.get("liquidity_level")
    if liquidity_level is not None:
        liquidity_level = str(liquidity_level) or None

    summary: dict[str, Any] = {
        "cycle": str(cycle or "unknown"),
        "domain_family": domain_family,
        "confidence": confidence_f,
        "profile_confidence": (
            str(pol.get("profile_confidence") or profile.get("profile_confidence") or "")
            or None
        ),
        "thin_campaign": (
            bool(pol.get("thin_campaign"))
            if pol.get("thin_campaign") is not None
            else (bool(profile.get("thin_campaign")) if profile else None)
        ),
        "input_richness": str(profile.get("input_richness") or "") or None,
        "liquidity_level": liquidity_level,
        "low_liquidity_market": bool(
            pol.get("low_liquidity_market")
            if pol.get("low_liquidity_market") is not None
            else profile.get("low_liquidity_market")
        )
        if (pol or profile)
        else None,
        "strictness_bias": strictness_bias_f,
        "threshold_adjustment": threshold_adj_f,
        "domain_threshold_delta": domain_delta_f,
        "domain_strictness_applied": bool(pol.get("domain_strictness_applied"))
        if pol.get("domain_strictness_applied") is not None
        else None,
        "prefilter_domain_softening": (
            bool(prefilter_domain_softening)
            if prefilter_domain_softening is not None
            else None
        ),
        "domain_tier_dropped": _int_or_none(domain_tier_dropped),
        "queries_dropped": queries_dropped,
        "queries_injected": queries_injected,
        "queries_boosted": queries_boosted,
        "queries_reordered": queries_reordered,
        "leads_promoted": _int_or_none(leads_promoted),
        "leads_scored_out": _int_or_none(leads_scored_out),
        "policy_mode": str(pol.get("mode")) if pol.get("mode") is not None else None,
        "policy_version": str(pol.get("policy_version"))
        if pol.get("policy_version") is not None
        else None,
        "override_active": bool(profile.get("override_active"))
        if profile.get("override_active") is not None
        else None,
    }

    if isinstance(extra, Mapping) and extra:
        for key, value in extra.items():
            # Do not clobber core keys with accidental extras.
            if key in summary:
                continue
            summary[str(key)] = value

    return summary


def domain_impact_for_scored_out(
    summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compact subset of a domain impact summary for ``scored_out`` lead docs.

    Keeps Firestore writes small while preserving the fields needed to explain
    why a lead may have failed the domain-aware promotion gate.
    """
    if not isinstance(summary, Mapping) or not summary:
        return {}
    keys = (
        "domain_family",
        "confidence",
        "profile_confidence",
        "thin_campaign",
        "strictness_bias",
        "threshold_adjustment",
        "domain_threshold_delta",
        "prefilter_domain_softening",
        "liquidity_level",
        "policy_mode",
        "leads_promoted",
        "leads_scored_out",
    )
    out: dict[str, Any] = {}
    for key in keys:
        if key in summary and summary[key] is not None:
            out[key] = summary[key]
    return out
