"""Unit tests for domain/strategy-aware Gemini prompt helpers (V26.6.0)."""
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if PIPELINE_ROOT not in sys.path:
    sys.path.insert(0, PIPELINE_ROOT)

# Lightweight package stubs so gemini_service can import core.* without full app boot.
for pkg_name, pkg_path in (
    ("services", str(Path(PIPELINE_ROOT) / "services")),
    ("core", str(Path(PIPELINE_ROOT) / "core")),
):
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [pkg_path]
        sys.modules[pkg_name] = pkg


def _load_gemini():
    module_path = Path(PIPELINE_ROOT) / "services" / "gemini_service.py"
    # Stub core modules used at import time
    if "core.logging" not in sys.modules:
        logging_mod = types.ModuleType("core.logging")
        logging_mod.get_logger = lambda name: types.SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        )
        sys.modules["core.logging"] = logging_mod
    if "core.clients" not in sys.modules:
        clients_mod = types.ModuleType("core.clients")
        clients_mod.init_vertex = lambda: None
        sys.modules["core.clients"] = clients_mod
    if "core.config" not in sys.modules:
        config_mod = types.ModuleType("core.config")
        config_mod.GEMINI_TIMEOUT_S = 45
        sys.modules["core.config"] = config_mod

    spec = importlib.util.spec_from_file_location("services.gemini_service", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["services.gemini_service"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_scoring_intent_rules_branch_by_strategy_and_vector():
    gs = _load_gemini()
    platform = gs._scoring_intent_rules("B2C", "PLATFORM_MINING", "real_estate")
    assert "PLATFORM_MINING FIT RULE" in platform
    assert "GENERIC B2B RULE" not in platform
    assert "overrides generic B2B" in platform.lower() or "PLATFORM_MINING" in platform

    consumer = gs._scoring_intent_rules("B2C", "COLLOQUIAL_DISCOVERY", "real_estate")
    assert "CONSUMER FIT RULE" in consumer
    assert "hiring intent" in consumer.lower() or "B2B hiring" in consumer

    b2b = gs._scoring_intent_rules("B2B", "COLLOQUIAL_DISCOVERY", "saas")
    assert "B2B INTENT FIT RULE" in b2b
    assert "4–6" in b2b or "4-6" in b2b  # softened ICP company band
    assert "SAAS" in b2b or "saas" in b2b.lower() or "DOMAIN NOTE" in b2b


def test_prefilter_step4_only_for_consumer_vectors():
    gs = _load_gemini()
    assert gs._prefilter_step4_consumer("B2B") == ""
    assert "CONSUMER CONTEXT-AWARE" in gs._prefilter_step4_consumer("B2C")
    assert "CONSUMER CONTEXT-AWARE" in gs._prefilter_step4_consumer("D2C")
    assert "CONSUMER CONTEXT-AWARE" in gs._prefilter_step4_consumer("B2B2C")


def test_platform_mining_forces_directory_softening_even_when_mode_inactive():
    gs = _load_gemini()
    thin_profile = {
        "domain_family": "real_estate",
        "profile_confidence": "low",
        "thin_campaign": True,
        "strictness_bias": -0.15,
        "liquidity_level": "low",
        "low_liquidity_market": True,
    }
    mode = gs._resolve_prefilter_domain_mode(thin_profile)
    # Thin/low conf normally disables softening
    assert not (mode and mode.get("soften_directories"))

    upgraded = gs._apply_strategy_to_prefilter_mode(mode, thin_profile, "PLATFORM_MINING")
    assert upgraded is not None
    assert upgraded.get("soften_directories") is True
    assert upgraded.get("active") is True
    assert upgraded.get("strategy_directory_softening") is True


def test_campaign_context_block_includes_domain_and_strategy():
    gs = _load_gemini()
    block = gs._campaign_context_block(
        domain_profile={
            "domain_family": "real_estate",
            "profile_confidence": "high",
            "liquidity_level": "low",
            "strictness_bias": -0.3,
            "thin_campaign": False,
        },
        sourcing_vector="B2C",
        primary_strategy="PLATFORM_MINING",
        enriched_context="PRODUCT/SERVICE: Oman Realty\nBUYER PAIN: unreliable agents",
    )
    assert "domain_family: real_estate" in block
    assert "sourcing_vector: B2C" in block
    assert "primary_strategy: PLATFORM_MINING" in block
    assert "profile_confidence: high" in block
    assert "liquidity_level: low" in block
    assert "Oman Realty" in block


def test_campaign_scoring_card_includes_rich_fields():
    gs = _load_gemini()
    card = gs._build_campaign_scoring_card(
        {
            "id": "c1",
            "bio": "Brand narrative firm",
            "keywords": "messaging, positioning",
            "pain_point": "inconsistent brand story",
            "target_angle_hook": "unified narrative",
            "unfair_advantage": "ex-agency operators",
            "sourcing_vector": "B2B",
            "effective_bio": "We help B2B companies craft brand narratives",
        },
        enriched_context="PRODUCT/SERVICE: Brand narrative\nBUYER HOOK: unified narrative",
    )
    assert card["campaign_id"] == "c1"
    assert card["pain_point"] == "inconsistent brand story"
    assert card["target_angle_hook"] == "unified narrative"
    assert card["unfair_advantage"] == "ex-agency operators"
    assert "enriched_icp_context" in card


def test_prefilter_few_shot_varies_by_domain():
    gs = _load_gemini()
    re = gs._prefilter_few_shot_guidance("real_estate", "B2C")
    assert "REAL ESTATE" in re or "Muscat" in re
    mkt = gs._prefilter_few_shot_guidance("marketing_agency", "B2B")
    assert "brand" in mkt.lower() or "MARKETING" in mkt
    assert re != mkt


def test_final_score_and_dm_prompt_includes_context(monkeypatch=None):
    gs = _load_gemini()
    captured = {}

    def _fake_call(prompt, expect_json=True, response_schema=None, system_instruction=None):
        captured["prompt"] = prompt
        captured["system"] = system_instruction
        return {
            "matched_campaigns": [{"campaign_id": "c1", "raw_score": 8}],
            "dm": "Hi there",
            "pain_point": "fragmented messaging",
            "icebreaker_angle": "saw your site",
            "intent_signal": "icp_match",
            "hiring_intent_found": "No",
            "tech_stack_found": [],
            "contact_endpoints": [],
            "decision_maker_name": "Unknown",
            "decision_maker_title": "Unknown",
            "company_size_tier": "Unknown",
            "primary_objection_hypothesis": "Unknown",
            "score_reasoning": "Strong ICP fit for brand narrative",
            "confidence_level": "HIGH",
            "evidence_chain": [],
            "company_name": "Acme",
        }

    gs.call_gemini_2_5 = _fake_call

    result = gs.final_score_and_dm(
        text="Acme Corp brand strategy page with contact form.",
        active_campaigns=[{
            "id": "c1",
            "bio": "Brand narrative development",
            "keywords": "brand, messaging",
            "pain_point": "inconsistent narrative",
            "target_angle_hook": "one story across markets",
            "sourcing_vector": "B2B",
            "intelligence_strategy": {"primary": "COLLOQUIAL_DISCOVERY"},
        }],
        context_payload="",
        tech_stack=[],
        source_url="https://acme.example/about",
        domain_profile={
            "domain_family": "marketing_agency",
            "profile_confidence": "medium",
            "liquidity_level": "high",
            "strictness_bias": 0.0,
        },
        sourcing_vector="B2B",
        primary_strategy="COLLOQUIAL_DISCOVERY",
        enriched_context="PRODUCT/SERVICE: Brand Narrative\nBUYER PAIN: inconsistent messaging",
    )

    prompt = captured["prompt"]
    assert "domain_family: marketing_agency" in prompt
    assert "sourcing_vector: B2B" in prompt
    assert "primary_strategy: COLLOQUIAL_DISCOVERY" in prompt
    assert "B2B INTENT FIT RULE" in prompt
    assert "GENERIC B2B RULE" not in prompt  # replaced by branched rule
    assert "inconsistent narrative" in prompt or "Brand Narrative" in prompt
    assert "MARKETING" in prompt or "brand" in prompt.lower()
    assert result["score"] == 8
    assert result["scoring_context"]["domain_family"] == "marketing_agency"
    assert result["scoring_context"]["intent_rules_mode"] == "b2b_default"


def test_final_score_platform_mining_uses_platform_rules():
    gs = _load_gemini()
    captured = {}

    def _fake_call(prompt, expect_json=True, response_schema=None, system_instruction=None):
        captured["prompt"] = prompt
        return {
            "matched_campaigns": [{"campaign_id": "oman", "raw_score": 7}],
            "dm": "Hi",
            "pain_point": "listing",
            "icebreaker_angle": "",
            "intent_signal": "",
            "hiring_intent_found": "No",
            "tech_stack_found": [],
            "contact_endpoints": [],
            "decision_maker_name": "Unknown",
            "decision_maker_title": "Unknown",
            "company_size_tier": "Unknown",
            "primary_objection_hypothesis": "Unknown",
            "score_reasoning": "Agent profile match",
            "confidence_level": "MEDIUM",
            "evidence_chain": [],
        }

    gs.call_gemini_2_5 = _fake_call
    result = gs.final_score_and_dm(
        text="Agent profile on Property Finder Muscat",
        active_campaigns=[{"id": "oman", "bio": "Oman Realty", "sourcing_vector": "B2C"}],
        context_payload="",
        tech_stack=[],
        source_url="https://propertyfinder.om/agent/1",
        domain_profile={"domain_family": "real_estate", "profile_confidence": "high", "liquidity_level": "low"},
        sourcing_vector="B2C",
        primary_strategy="PLATFORM_MINING",
    )
    assert "PLATFORM_MINING FIT RULE" in captured["prompt"]
    assert "REAL ESTATE" in captured["prompt"]
    assert result["scoring_context"]["intent_rules_mode"] == "platform_mining"


def test_pre_filter_prompt_strategy_and_consumer_gating():
    gs = _load_gemini()
    captured = {}

    def _fake_call(prompt, expect_json=True, response_schema=None, system_instruction=None):
        captured["prompt"] = prompt
        return [
            {"url": "https://propertyfinder.om/agent/1", "confidence_tier": "High", "reason": "listing"},
            {"url": "https://example.com/top-10-agents", "confidence_tier": "Low", "reason": "listicle"},
        ]

    gs.call_gemini_2_5 = _fake_call
    out = gs.pre_filter_gemini(
        snippets=[
            {"link": "https://propertyfinder.om/agent/1", "title": "Agent", "snippet": "Muscat villas"},
            {"link": "https://example.com/top-10-agents", "title": "Top 10", "snippet": "best agencies"},
        ],
        bio="Oman Realty local agents",
        location_target="Muscat, Oman",
        domain_profile={
            "domain_family": "real_estate",
            "profile_confidence": "high",
            "liquidity_level": "low",
            "low_liquidity_market": True,
            "strictness_bias": -0.3,
        },
        sourcing_vector="B2C",
        primary_strategy="PLATFORM_MINING",
    )
    prompt = captured["prompt"]
    assert "primary_strategy: PLATFORM_MINING" in prompt
    assert "domain_family: real_estate" in prompt
    assert "STRATEGY OVERRIDE — PLATFORM_MINING" in prompt
    assert "STEP 4 — CONSUMER CONTEXT-AWARE" in prompt
    assert "REAL ESTATE" in prompt or "Muscat" in prompt
    assert "https://propertyfinder.om/agent/1" in out["High"]


def test_pre_filter_b2b_does_not_include_consumer_step4():
    gs = _load_gemini()
    captured = {}

    def _fake_call(prompt, expect_json=True, response_schema=None, system_instruction=None):
        captured["prompt"] = prompt
        return [{"url": "https://reddit.com/r/sales/1", "confidence_tier": "High", "reason": "pain"}]

    gs.call_gemini_2_5 = _fake_call
    gs.pre_filter_gemini(
        snippets=[{"link": "https://reddit.com/r/sales/1", "title": "x", "snippet": "need help"}],
        bio="B2B SaaS CRM",
        location_target="US",
        domain_profile={"domain_family": "saas", "profile_confidence": "high"},
        sourcing_vector="B2B",
        primary_strategy="COLLOQUIAL_DISCOVERY",
    )
    assert "STEP 4 — CONSUMER CONTEXT-AWARE" not in captured["prompt"]
    assert "sourcing_vector: B2B" in captured["prompt"]
