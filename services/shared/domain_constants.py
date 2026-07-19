"""Canonical domain-family constants for LeadGenie domain intelligence.

Single source of truth used by:
  - pipeline-main ``services.domain_intelligence``
  - orchestrator campaigns API (domain_override validation)

Do not redefine family allowlists elsewhere — import from here.

Education sub-patterns, preferred platforms, and entity language packs live in
``shared.education_profiles`` (not here) so family allowlists stay lean.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Supported domain families (must stay aligned with inference term packs +
# family-default maps in pipeline domain_intelligence).
# ---------------------------------------------------------------------------
KNOWN_DOMAIN_FAMILIES: frozenset[str] = frozenset({
    "real_estate",
    "saas",
    "manufacturing",
    "professional_services",
    "healthcare",
    "education",
    "finance",
    "ecommerce",
    "hospitality",
    "logistics",
    "construction",
    "hr_recruiting",
    "marketing_agency",
    "general_services",
})

# Common human / product aliases → canonical family keys.
DOMAIN_FAMILY_ALIASES: dict[str, str] = {
    "realestate": "real_estate",
    "property": "real_estate",
    "proptech": "real_estate",
    "b2b_saas": "saas",
    "software": "saas",
    "pro_services": "professional_services",
    "professional": "professional_services",
    "health": "healthcare",
    "med": "healthcare",
    "edu": "education",
    "fintech": "finance",
    "e_commerce": "ecommerce",
    "ecom": "ecommerce",
    "hr": "hr_recruiting",
    "recruiting": "hr_recruiting",
    "agency": "marketing_agency",
    "marketing": "marketing_agency",
    "general": "general_services",
    "other": "general_services",
}

# Fields accepted on campaign.domain_override (partial profiles OK).
DOMAIN_OVERRIDE_ALLOWED_KEYS: frozenset[str] = frozenset({
    "domain_family",
    "confidence",
    "liquidity_level",
    "low_liquidity_market",
    "preferred_sources",
    "preferred_query_hints",
    "blocked_subreddits",
    "strictness_bias",
    "notes",
})

LIQUIDITY_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})


def is_valid_domain_family(value: Any) -> bool:
    """Return True if *value* is a known canonical domain family key.

    Does **not** resolve aliases — use ``normalize_domain_family`` first when
    accepting user input.
    """
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in KNOWN_DOMAIN_FAMILIES


def normalize_domain_family(value: Any) -> str | None:
    """Normalize user/API input to a canonical domain family, or None if invalid.

    Handles whitespace, hyphen/space → underscore, and known aliases.
    """
    if value is None:
        return None
    text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if not text:
        return None
    text = DOMAIN_FAMILY_ALIASES.get(text, text)
    if text not in KNOWN_DOMAIN_FAMILIES:
        return None
    return text


def allowed_domain_families_csv() -> str:
    """Sorted comma-separated list for error messages."""
    return ", ".join(sorted(KNOWN_DOMAIN_FAMILIES))
