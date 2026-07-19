"""Unified domain platform config + profile contract (domain-v4).

Proves:
  - education + study_abroad → correct platforms + language pack
  - real_estate still uses directory_listing (agent/broker)
  - a new vertical can be registered with config-only changes
  - platform mining never invents agent/broker without directory pack
  - no dependency on per-family education_profiles business logic
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main")
)
for path in (ROOT, PIPELINE_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from shared.domain_platform_config import (  # noqa: E402
    ENTITY_LANGUAGE_PACKS,
    FAMILY_PLATFORM_CONFIG,
    SUB_PATTERN_CONFIG,
    detect_sub_pattern,
    get_entity_terms,
    query_uses_directory_only_language,
    resolve_platform_slice,
)
from services.domain_intelligence import (  # noqa: E402
    DOMAIN_PROFILE_VERSION,
    _build_query_from_hint,
    infer_domain_profile,
)
from services.query_brain import (  # noqa: E402
    _platform_mining_deterministic_fallback,
    _resolve_platform_entity_language,
)


MBBS_CAMPAIGN = {
    "name": "MBBS Georgia — Kerala Student Outreach",
    "bio": (
        "Help Indian students find trusted education consultants for MBBS "
        "abroad in Georgia. Study abroad medical education counselling."
    ),
    "keywords": "MBBS, study abroad, Georgia, education consultant, admission",
    "location": "Kerala, India, Asia",
    "sourcing_vector": "B2C",
    "intelligence_strategy": {"primary": "COLLOQUIAL_DISCOVERY"},
}

REAL_ESTATE_CAMPAIGN = {
    "name": "Oman Realty Prospecting",
    "bio": "Help buyers find trusted property brokers for apartments and villas.",
    "keywords": "real estate, property agent, apartment, villa",
    "location": "Muscat, Oman",
    "sourcing_vector": "B2C",
}


def test_profile_contract_fields_present_education():
    profile = infer_domain_profile(MBBS_CAMPAIGN)
    assert profile["domain_family"] == "education"
    assert profile["version"] == DOMAIN_PROFILE_VERSION
    assert profile.get("sub_pattern") == "study_abroad"
    assert profile.get("entity_language_pack") == "education_study_abroad"
    assert profile.get("platform_mining_mode") == "consumer"
    assert isinstance(profile.get("entity_terms"), list) and profile["entity_terms"]
    assert isinstance(profile.get("preferred_query_hints"), list)
    # Compat alias
    assert profile.get("education_sub_pattern") == "study_abroad"


def test_education_study_abroad_platforms_and_language():
    profile = infer_domain_profile(MBBS_CAMPAIGN)
    blob = " ".join(str(h).lower() for h in profile["preferred_query_hints"])
    assert "r/teachers" not in blob
    assert "coursera.org" not in blob
    assert "linkedin.com" not in blob
    assert any(x in blob for x in ("reddit", "quora", "youtube", "shiksha"))
    terms = " ".join(profile["entity_terms"]).lower()
    assert "broker" not in terms
    assert any(t in terms for t in ("consultant", "counsellor", "admission"))


def test_real_estate_still_directory_listing_agent_broker():
    profile = infer_domain_profile(REAL_ESTATE_CAMPAIGN)
    assert profile["domain_family"] == "real_estate"
    assert profile.get("entity_language_pack") == "directory_listing"
    assert profile.get("platform_mining_mode") == "directory"
    terms = profile.get("entity_terms") or []
    assert "agent" in terms and "broker" in terms
    blob = " ".join(str(h).lower() for h in profile["preferred_query_hints"])
    assert "bayut" in blob or "propertyfinder" in blob or "dubizzle" in blob


def test_platform_mining_education_no_agent_broker():
    profile = infer_domain_profile(MBBS_CAMPAIGN)
    queries = _platform_mining_deterministic_fallback(
        campaign_id="edu-cfg",
        strategy_plan={
            "geo_terms": ["Kerala", "India"],
            "primary_strategy": "COLLOQUIAL_DISCOVERY",
        },
        domain_profile=profile,
        domain_targets=[],
        domain_family="education",
        reason="unit",
        sourcing_vector="B2C",
        primary_strategy="COLLOQUIAL_DISCOVERY",
    )
    assert queries
    for q in queries:
        assert not query_uses_directory_only_language(q), q
        assert "agent broker" not in q.lower()


def test_platform_mining_real_estate_keeps_agent_broker():
    profile = infer_domain_profile(REAL_ESTATE_CAMPAIGN)
    queries = _platform_mining_deterministic_fallback(
        campaign_id="re-cfg",
        strategy_plan={"geo_terms": ["Muscat"], "platform_targets": ["bayut.com"]},
        domain_profile=profile,
        domain_targets=["bayut.com"],
        domain_family="real_estate",
        reason="unit",
        sourcing_vector="B2C",
        primary_strategy="PLATFORM_MINING",
    )
    joined = " ".join(queries).lower()
    assert "agent" in joined or "broker" in joined
    assert "bayut" in joined


def test_resolve_language_from_profile_not_family_hardcode():
    """Language must come from pack name, not if family == real_estate branches."""
    profile = {
        "domain_family": "education",
        "entity_language_pack": "education_study_abroad",
        "entity_terms": ["consultant", "counsellor", "admission"],
        "platform_mining_mode": "consumer",
    }
    terms, pack = _resolve_platform_entity_language(
        domain_family="education",
        domain_profile=profile,
        host="reddit.com",
        sourcing_vector="B2C",
    )
    assert pack == "education_study_abroad"
    assert "consultant" in terms
    assert "broker" not in terms


def test_missing_profile_uses_neutral_safe_not_agent_broker():
    terms, pack = _resolve_platform_entity_language(
        domain_family="unknown_xyz",
        domain_profile=None,
        host="",
        sourcing_vector="B2C",
    )
    # unknown family → general_services or neutral — never invent agent broker
    joined = " ".join(terms).lower()
    assert not ("agent" in joined and "broker" in joined)
    assert pack != "legacy_default"


def test_config_only_new_vertical_without_new_python_module(monkeypatch):
    """Adding a vertical is a FAMILY_PLATFORM_CONFIG data change only."""
    # Register a hypothetical vertical purely in config tables.
    FAMILY_PLATFORM_CONFIG["pet_services"] = {
        "preferred_sources": ("serper_discovery", "reddit", "google_reviews"),
        "preferred_query_hints": (
            "site:reddit.com/r/pets",
            "site:yelp.com",
            "site:facebook.com/groups",
        ),
        "platform_hosts": ("reddit.com", "yelp.com", "facebook.com"),
        "entity_language_pack": "consumer_discovery",
        "platform_mining_mode": "consumer",
    }
    try:
        slice_ = resolve_platform_slice("pet_services", sourcing_vector="B2C")
        assert slice_["entity_language_pack"] == "consumer_discovery"
        assert slice_["platform_mining_mode"] == "consumer"
        assert "yelp.com" in slice_["platform_hosts"]
        terms = get_entity_terms(slice_["entity_language_pack"])
        assert "looking for" in terms or "recommend" in terms
        assert "broker" not in terms

        # Platform mining from a synthetic profile uses config pack only.
        profile = {
            "domain_family": "pet_services",
            "entity_language_pack": slice_["entity_language_pack"],
            "entity_terms": terms,
            "platform_mining_mode": "consumer",
            "platform_hosts": slice_["platform_hosts"],
            "preferred_query_hints": slice_["preferred_query_hints"],
        }
        queries = _platform_mining_deterministic_fallback(
            campaign_id="pets",
            strategy_plan={"geo_terms": ["Austin"]},
            domain_profile=profile,
            domain_targets=[],
            domain_family="pet_services",
            reason="config_only",
            sourcing_vector="B2C",
        )
        assert queries
        for q in queries:
            assert not query_uses_directory_only_language(q), q
    finally:
        FAMILY_PLATFORM_CONFIG.pop("pet_services", None)


def test_sub_pattern_detection_is_table_driven():
    sub, conf, matched = detect_sub_pattern("education", MBBS_CAMPAIGN)
    assert sub == "study_abroad"
    assert conf > 0.4
    assert matched
    # Families without SUB_PATTERN_CONFIG return None
    sub2, _, _ = detect_sub_pattern("real_estate", REAL_ESTATE_CAMPAIGN)
    assert sub2 is None


def test_build_query_from_hint_uses_profile_pack():
    profile = infer_domain_profile(MBBS_CAMPAIGN)
    q = _build_query_from_hint(
        "site:reddit.com",
        family="education",
        location="Kerala",
        keywords="MBBS",
        domain_profile=profile,
    )
    assert "site:reddit.com" in q.lower()
    assert not query_uses_directory_only_language(q)


def test_entity_language_packs_are_reusable_data():
    assert "directory_listing" in ENTITY_LANGUAGE_PACKS
    assert "education_study_abroad" in ENTITY_LANGUAGE_PACKS
    assert "neutral_safe" in ENTITY_LANGUAGE_PACKS
    assert "education" in SUB_PATTERN_CONFIG
    assert "study_abroad" in SUB_PATTERN_CONFIG["education"]["patterns"]


def test_education_profiles_shim_delegates():
    """Deprecated shim still works but is not the SSOT."""
    from shared.education_profiles import resolve_education_profile

    edu = resolve_education_profile(MBBS_CAMPAIGN, sourcing_vector="B2C")
    assert edu["education_sub_pattern"] == "study_abroad"
    assert edu.get("entity_language_pack") == "education_study_abroad"
