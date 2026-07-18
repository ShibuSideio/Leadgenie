"""V27 IntentDomainOrchestrator — real-life use case tests.

Covers CEO requirements:
  1. Public-data only posture (channel matrix)
  2. Smart query knobs from sparse input
  3. No hard domain blocks for G2/Capterra/etc.
  4. High-yield channel admission
  5. Nourish plan per use case
  6. Real-life auto-adapt (Oman RE, Kerala SaaS CAC, brand narrative)
  7. Fail-open + flag BC
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main")
)
for path in (ROOT, PIPELINE_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from shared.intent_orchestrator import (  # noqa: E402
    USE_CASE_BRAND_NARRATIVE,
    USE_CASE_CAC_COMPETITOR_TOUCHPOINT,
    USE_CASE_PLATFORM_BUYER_MINING,
    USE_CASE_SCAM_RECOVERY,
    build_intent_profile,
    channel_is_admissible,
    env_v27_flag,
    funnel_snapshot,
    is_v27_orchestrator_enabled,
    nourish_plan_for_profile,
    should_hard_drop_result,
    v27_flag_diagnostics,
)
from services.query_governance import govern_query_portfolio  # noqa: E402
from services.serper_service import filter_serper_noise  # noqa: E402


# ---------------------------------------------------------------------------
# Flag / BC
# ---------------------------------------------------------------------------

def test_flag_default_off():
    assert is_v27_orchestrator_enabled({}) is False
    assert is_v27_orchestrator_enabled(env={"V27_INTELLIGENCE_ORCHESTRATOR": "false"}) is False


def test_flag_env_on():
    assert is_v27_orchestrator_enabled(env={"V27_INTELLIGENCE_ORCHESTRATOR": "true"}) is True


def test_flag_campaign_override():
    assert is_v27_orchestrator_enabled(
        {"flags": {"v27_intelligence_orchestrator": True}}
    ) is True
    assert is_v27_orchestrator_enabled(
        {"flags": {"v27_intelligence_orchestrator": False}},
        env={"V27_INTELLIGENCE_ORCHESTRATOR": "true"},
    ) is False


def test_flag_null_campaign_does_not_suppress_env():
    """Regression: flags.v27=null must fall through to env=true (prod bug)."""
    assert is_v27_orchestrator_enabled(
        {"flags": {"v27_intelligence_orchestrator": None}},
        env={"V27_INTELLIGENCE_ORCHESTRATOR": "true"},
    ) is True


def test_flag_quoted_env_true():
    assert is_v27_orchestrator_enabled(
        env={"V27_INTELLIGENCE_ORCHESTRATOR": '"true"'}
    ) is True
    assert is_v27_orchestrator_enabled(
        env={"V27_INTELLIGENCE_ORCHESTRATOR": "'true'"}
    ) is True


def test_env_v27_flag_and_diagnostics():
    on, raw = env_v27_flag({"V27_INTELLIGENCE_ORCHESTRATOR": "true"})
    assert on is True
    assert raw == "true"
    diag = v27_flag_diagnostics(
        {"flags": {}},
        env={"V27_INTELLIGENCE_ORCHESTRATOR": "true"},
    )
    assert diag["enabled"] is True
    assert diag["env_enabled"] is True


def test_shared_package_import_always_available():
    """Pipeline Docker image always has shared/ — this is the production import path."""
    import shared.intent_orchestrator as mod
    assert hasattr(mod, "is_v27_orchestrator_enabled")
    assert mod.is_v27_orchestrator_enabled(env={"V27_INTELLIGENCE_ORCHESTRATOR": "1"}) is True


def test_build_inactive_when_flag_off():
    profile = build_intent_profile(
        {"name": "Oman Realty", "sourcing_vector": "B2C"},
        {"domain_family": "real_estate", "liquidity_level": "low", "low_liquidity_market": True},
        force_enabled=False,
    )
    assert profile.orchestrator_active is False


# ---------------------------------------------------------------------------
# Real-life use cases
# ---------------------------------------------------------------------------

def test_oman_real_estate_platform_mining():
    campaign = {
        "name": "Oman Realty",
        "bio": "Property agents and brokers in Muscat, Oman",
        "keywords": "villa, apartment, property for sale, agent",
        "location": "Muscat, Oman",
        "sourcing_vector": "B2C",
        "pain_point": "buyers looking for trusted property agents",
        "intelligence_strategy": {"primary": "PLATFORM_MINING"},
        "flags": {"v27_intelligence_orchestrator": True},
    }
    domain = {
        "domain_family": "real_estate",
        "liquidity_level": "low",
        "low_liquidity_market": True,
        "profile_confidence": "high",
    }
    profile = build_intent_profile(campaign, domain, force_enabled=True)

    assert profile.orchestrator_active is True
    assert profile.use_case in (USE_CASE_PLATFORM_BUYER_MINING, USE_CASE_SCAM_RECOVERY)
    assert profile.platform_mining_level == "force"
    assert profile.force_geo_global_fallback is True
    assert profile.max_site_exclusions <= 4  # low liquidity
    assert profile.entity_extraction_enabled is True
    assert "g2.com" in profile.never_block_domains or "bayut.com" in profile.never_block_domains
    assert any(c in profile.channel_priority for c in ("directories", "trustpilot", "reddit"))
    assert profile.nourish_depth in ("entity_first", "deep")


def test_scam_detection_forces_platform_mining():
    campaign = {
        "name": "Dubai Property Watch",
        "bio": "Help buyers avoid fake agents and broker scams",
        "pain_point": "scam agent stole deposit hidden fees",
        "keywords": "fake listing, dishonest broker",
        "sourcing_vector": "B2C",
        "location": "Dubai, UAE",
    }
    profile = build_intent_profile(
        campaign,
        {"domain_family": "real_estate", "liquidity_level": "medium"},
        force_enabled=True,
    )
    assert profile.use_case == USE_CASE_SCAM_RECOVERY
    assert profile.platform_mining_level == "force"
    assert profile.primary_strategy == "PLATFORM_MINING"
    assert profile.buyer_intent == "high"


def test_kerala_saas_cac_competitor_touchpoint():
    campaign = {
        "name": "Kerala SaaS Growth",
        "bio": "B2B CRM for mid-market sales teams in Kerala",
        "pain_point": "high CAC and customers looking for alternative to expensive CRM",
        "keywords": "customer acquisition cost, switching from HubSpot, churn",
        "sourcing_vector": "B2B",
        "location": "Kochi, Kerala, India",
        "intelligence_strategy": {"primary": "COLLOQUIAL_DISCOVERY"},
    }
    domain = {
        "domain_family": "saas",
        "liquidity_level": "high",
        "profile_confidence": "high",
    }
    profile = build_intent_profile(campaign, domain, force_enabled=True)

    assert profile.use_case == USE_CASE_CAC_COMPETITOR_TOUCHPOINT
    assert profile.primary_strategy == "COMPETITOR_TOUCHPOINT"
    assert "g2" in profile.channel_priority
    assert "capterra" in profile.channel_priority
    assert profile.entity_extraction_enabled is True
    assert profile.soft_directory_prefilter is True
    plan = nourish_plan_for_profile(profile)
    assert plan.get("entity_extraction") is True
    assert plan.get("priority") in ("realtime", "batch")


def test_brand_narrative_marketing_agency():
    campaign = {
        "name": "Brand Narrative Studio",
        "bio": "Brand narrative development and brand positioning for FMCG",
        "keywords": "brand identity, brand architecture, storytelling, retail marketing",
        "sourcing_vector": "B2B",
        "location": "India",
        "pain_point": "CMOs need clearer brand voice",
    }
    domain = {
        "domain_family": "marketing_agency",
        "liquidity_level": "medium",
        "profile_confidence": "medium",
    }
    profile = build_intent_profile(campaign, domain, force_enabled=True)

    assert profile.use_case == USE_CASE_BRAND_NARRATIVE
    assert profile.domain_family == "marketing_agency"
    assert "linkedin" in profile.channel_priority
    assert profile.nourish_depth in ("standard", "deep")


# ---------------------------------------------------------------------------
# Channel admission — no hard domain blocks
# ---------------------------------------------------------------------------

def test_g2_capterra_never_hard_blocked_under_v27():
    profile = build_intent_profile(
        {
            "name": "SaaS",
            "bio": "sales software",
            "sourcing_vector": "B2B",
            "pain_point": "looking for alternative CRM high CAC",
        },
        {"domain_family": "saas"},
        force_enabled=True,
    )
    for domain in ("g2.com", "capterra.com", "trustpilot.com", "reddit.com", "quora.com"):
        assert channel_is_admissible(domain, profile) is True

    g2_result = {
        "link": "https://www.g2.com/products/example/reviews",
        "title": "Example Reviews",
        "snippet": "Users say switching reduced cost",
    }
    drop, reason = should_hard_drop_result(
        g2_result,
        profile,
        legacy_enterprise_domains=["g2.com", "capterra.com", "ibm.com"],
    )
    assert drop is False, f"G2 should not hard-drop, got reason={reason}"

    author_result = {
        "link": "https://www.g2.com/author/jane-doe",
        "title": "Author page",
        "snippet": "About the author",
    }
    drop_author, reason_author = should_hard_drop_result(author_result, profile)
    assert drop_author is True
    assert "path_exclude" in reason_author


def test_filter_serper_noise_admits_g2_when_v27_active():
    profile = build_intent_profile(
        {
            "name": "SaaS CAC",
            "bio": "CRM alternative",
            "pain_point": "high CAC switching from competitor",
            "sourcing_vector": "B2B",
        },
        {"domain_family": "saas"},
        force_enabled=True,
    )
    results = [
        {
            "link": "https://www.g2.com/products/foo/reviews",
            "title": "Foo reviews",
            "snippet": "Great alternative after pricing increase",
        },
        {
            "link": "https://www.capterra.com/p/123/bar/",
            "title": "Bar on Capterra",
            "snippet": "Users comparing vendors",
        },
        {
            "link": "https://www.ibm.com/products/something",
            "title": "IBM product",
            "snippet": "Enterprise brochure",
        },
    ]
    cleaned = filter_serper_noise(results, intent_profile=profile.to_dict())
    links = [r["link"] for r in cleaned]
    assert any("g2.com" in u for u in links)
    assert any("capterra.com" in u for u in links)
    # ibm mega-vendor still dropped as non-channel enterprise noise
    assert not any("ibm.com" in u for u in links)


def test_filter_serper_noise_legacy_still_blocks_g2_without_profile():
    results = [
        {
            "link": "https://www.g2.com/products/foo/reviews",
            "title": "Foo",
            "snippet": "Reviews",
        },
    ]
    cleaned = filter_serper_noise(results)  # no intent_profile
    assert cleaned == []


# ---------------------------------------------------------------------------
# Governance consumes intent profile
# ---------------------------------------------------------------------------

def test_governance_uses_intent_platform_force():
    campaign = {
        "keywords": "property Oman villa",
        "intelligence_strategy": {
            "primary": "COLLOQUIAL_DISCOVERY",
            "platform_targets": ["Bayut", "Property Finder"],
        },
        "system_domain_profile": {
            "domain_family": "real_estate",
            "liquidity_level": "low",
            "low_liquidity_market": True,
        },
    }
    profile = build_intent_profile(
        campaign,
        campaign["system_domain_profile"],
        force_enabled=True,
    )
    candidates = [
        '"trusted property agents oman"',
        '"muscat villa looking for"',
        '"oman apartment near me"',
    ]
    result = govern_query_portfolio(
        candidates,
        campaign=campaign,
        sourcing_vector="B2C",
        location="Muscat, Oman",
        domain_profile=campaign["system_domain_profile"],
        intent_profile=profile.to_dict(),
    )
    governed = result["queries"]
    assert result["stats"].get("v27_orchestrator") is True
    # Force platform mining should inject site: queries
    assert any("site:" in q for q in governed)
    assert result["stats"]["platform_injected"] >= 1 or any("site:" in q for q in governed)


# ---------------------------------------------------------------------------
# Funnel + nourish
# ---------------------------------------------------------------------------

def test_funnel_snapshot_shape():
    profile = build_intent_profile(
        {"name": "x", "sourcing_vector": "B2B"},
        {"domain_family": "saas"},
        force_enabled=True,
    )
    snap = funnel_snapshot(
        intent_profile=profile,
        queries_executed=8,
        raw_hits=40,
        after_noise=30,
        after_stale=28,
        queued=12,
        geo_fallbacks_attempted=2,
        geo_fallbacks_succeeded=1,
        platform_queries_executed=4,
        noise_dropped=10,
        channel_admitted=15,
    )
    assert snap["version"] == "funnel-v1"
    assert snap["orchestrator_active"] is True
    assert snap["queries_executed"] == 8
    assert snap["queued"] == 12
    assert snap["use_case"]


def test_nourish_plan_entity_first_for_platform():
    profile = build_intent_profile(
        {
            "name": "Oman Realty",
            "bio": "real estate agents",
            "keywords": "property villa agent",
            "sourcing_vector": "B2C",
        },
        {"domain_family": "real_estate", "low_liquidity_market": True, "liquidity_level": "low"},
        force_enabled=True,
    )
    plan = nourish_plan_for_profile(profile)
    assert plan.get("entity_extraction") is True
    assert plan.get("status_on_thin") == "enrichment_pending"
    assert "company_name" in (plan.get("required_fields") or []) or plan.get("public_contact_harvest")
