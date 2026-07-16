import importlib.util
import os
import sys
import types
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if PIPELINE_ROOT not in sys.path:
    sys.path.insert(0, PIPELINE_ROOT)

services_pkg = types.ModuleType("services")
services_pkg.__path__ = [str(Path(PIPELINE_ROOT) / "services")]
sys.modules["services"] = services_pkg


def _load_module(module_name: str, relative_path: str):
    module_path = Path(PIPELINE_ROOT) / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_confidence_gate_promotes_high_intent_signals():
    lead_confidence = _load_module("services.lead_confidence", "services/lead_confidence.py")
    bundle = lead_confidence.calculate_lead_confidence(
        evaluation={"score": 7, "tier": "HIGH", "topic_coherence": 0.9, "pain_summary": "Need a CRM", "contact_point": "@user"},
        text="We are looking for a CRM because our lead scoring is broken and we need help urgently",
        url="https://reddit.com/r/sales/comments/123",
        source_tier="High",
        is_harvest_lead=True,
    )
    assert bundle["promotion"] is True
    assert bundle["confidence_score"] >= 70


def test_confidence_gate_blocks_weak_signals():
    lead_confidence = _load_module("services.lead_confidence", "services/lead_confidence.py")
    bundle = lead_confidence.calculate_lead_confidence(
        evaluation={"score": 3, "tier": "LOW", "topic_coherence": 0.1},
        text="A general article about marketing automation trends",
        url="https://example.com/blog/marketing-automation",
        source_tier="Low",
        is_harvest_lead=False,
        is_thin_payload=True,
    )
    assert bundle["promotion"] is False


def test_confidence_threshold_adjustment_relaxes_gate_in_recovery():
    lead_confidence = _load_module("services.lead_confidence", "services/lead_confidence.py")
    baseline = lead_confidence.calculate_lead_confidence(
        evaluation={"score": 6, "tier": "MEDIUM", "topic_coherence": 0.62, "pain_summary": "Need better pipeline"},
        text="Need help improving lead flow and recommendations for tools",
        url="https://forum.example.com/thread",
        source_tier="Medium",
        is_harvest_lead=False,
        threshold_adjustment=0.0,
    )
    relaxed = lead_confidence.calculate_lead_confidence(
        evaluation={"score": 6, "tier": "MEDIUM", "topic_coherence": 0.62, "pain_summary": "Need better pipeline"},
        text="Need help improving lead flow and recommendations for tools",
        url="https://forum.example.com/thread",
        source_tier="Medium",
        is_harvest_lead=False,
        threshold_adjustment=-8.0,
    )
    assert relaxed["confidence_threshold"] < baseline["confidence_threshold"]


# ---------------------------------------------------------------------------
# V26.5.1: Adapter + hybrid promotion
# ---------------------------------------------------------------------------

def test_adapter_maps_final_score_and_dm_schema():
    """final_score_and_dm output must produce harvest-style confidence fields."""
    lc = _load_module("services.lead_confidence", "services/lead_confidence.py")
    raw = {
        "score": 8,
        "confidence_level": "HIGH",
        "pain_point": "Inconsistent brand messaging across markets",
        "contact_endpoints": [{"platform": "email", "uri": "cmo@acme.com"}],
        "company_name": "Acme Corp",
        # Deliberately omit tier / topic_coherence / pain_summary / contact_point
    }
    adapted = lc.adapt_gemini_evaluation_for_confidence(raw)

    assert adapted["tier"] == "HIGH"
    assert adapted["topic_coherence"] >= 0.75
    assert adapted["pain_summary"] == "Inconsistent brand messaging across markets"
    assert adapted["contact_point"] == "cmo@acme.com"
    assert adapted["geo_match"] is True
    assert adapted["_adapter_used"] is True
    assert adapted["_harvest_fields_present"] is False
    assert adapted["_adapter_source"] == "gemini_score"


def test_adapter_preserves_harvest_fields():
    """Harvest inline_score_signal fields must not be overwritten by empty Gemini keys."""
    lc = _load_module("services.lead_confidence", "services/lead_confidence.py")
    harvest = {
        "score": 5,
        "tier": "HIGH",
        "topic_coherence": 0.92,
        "pain_summary": "Need a CRM now",
        "contact_point": "@buyer",
        "geo_match": False,
        "confidence_level": "MEDIUM",
    }
    adapted = lc.adapt_gemini_evaluation_for_confidence(harvest)

    assert adapted["tier"] == "HIGH"
    assert adapted["topic_coherence"] == 0.92
    assert adapted["pain_summary"] == "Need a CRM now"
    assert adapted["contact_point"] == "@buyer"
    assert adapted["geo_match"] is False
    assert adapted["_harvest_fields_present"] is True


def test_adapter_empty_evaluation_is_safe():
    lc = _load_module("services.lead_confidence", "services/lead_confidence.py")
    adapted = lc.adapt_gemini_evaluation_for_confidence(None)
    assert adapted["tier"] == "LOW"
    assert adapted["topic_coherence"] == 0.0
    assert adapted["score"] == 0
    assert adapted["_adapter_source"] == "empty"

    bundle = lc.calculate_lead_confidence(evaluation=adapted, text="", url="")
    assert bundle["promotion"] is False


def test_serper_path_high_gemini_score_promotes_via_adapter():
    """Regression: company page with Gemini score=8 and no buyer keywords must promote."""
    lc = _load_module("services.lead_confidence", "services/lead_confidence.py")
    raw = {
        "score": 8,
        "confidence_level": "HIGH",
        "pain_point": "Brand narrative is fragmented across teams",
        "contact_endpoints": [],
        "company_name": "Northstar Media",
    }
    adapted = lc.adapt_gemini_evaluation_for_confidence(raw)
    bundle = lc.calculate_lead_confidence(
        evaluation=adapted,
        text="Northstar Media provides industrial solutions established in 2010. Contact us.",
        url="https://northstar-media.example/about",
        source_tier="High",
        is_harvest_lead=False,
        is_thin_payload=False,
        threshold_adjustment=0.0,
    )
    # Without adapter this was conf≈10 and always failed. With adapter it must clear 62.
    assert bundle["confidence_score"] >= 62
    assert bundle["promotion"] is True
    assert adapted["tier"] == "HIGH"


def test_serper_path_low_gemini_score_still_rejects():
    lc = _load_module("services.lead_confidence", "services/lead_confidence.py")
    raw = {
        "score": 2,
        "confidence_level": "SPECULATIVE",
        "pain_point": "Unknown",
        "company_name": "Unknown",
    }
    adapted = lc.adapt_gemini_evaluation_for_confidence(raw)
    bundle = lc.calculate_lead_confidence(
        evaluation=adapted,
        text="A general directory listing of companies in the region.",
        url="https://directory.example/list",
        source_tier="Medium",
        is_thin_payload=False,
    )
    assert bundle["promotion"] is False


def test_hybrid_promotion_triggers_on_score_floor_when_confidence_fails():
    """Hybrid path: high Gemini score + basic signals promotes even if conf is below thr."""
    lc = _load_module("services.lead_confidence", "services/lead_confidence.py")
    # Force a weak confidence bundle, then apply hybrid with score=8 balanced floor.
    weak = {
        "confidence_score": 40.0,
        "confidence_threshold": 62.0,
        "promotion": False,
        "reason": "weak-evidence fallback",
    }
    adapted = lc.adapt_gemini_evaluation_for_confidence({
        "score": 8,
        "confidence_level": "HIGH",
        "pain_point": "Need messaging framework",
        "company_name": "Acme",
    })
    result = lc.apply_hybrid_promotion(
        weak,
        gemini_score=8,
        policy_mode="balanced",
        is_thin_payload=False,
        adapted_evaluation=adapted,
        enabled=True,
    )
    assert result["promotion"] is True
    assert result["hybrid_promotion_triggered"] is True
    assert result["promotion_path"] == "hybrid_score"
    assert result["hybrid_score_floor"] == lc.HYBRID_SCORE_FLOOR_BALANCED
    assert "hybrid-score-floor" in result["reason"]


def test_hybrid_promotion_recovery_uses_lower_floor():
    lc = _load_module("services.lead_confidence", "services/lead_confidence.py")
    adapted = lc.adapt_gemini_evaluation_for_confidence({
        "score": 7,
        "confidence_level": "MEDIUM",
        "pain_point": "Looking for local agents",
        "company_name": "Oman Homes",
    })
    weak = {"confidence_score": 30.0, "confidence_threshold": 56.0, "promotion": False, "reason": "weak-evidence fallback"}
    balanced = lc.apply_hybrid_promotion(
        weak, gemini_score=7, policy_mode="balanced", adapted_evaluation=adapted, enabled=True
    )
    recovery = lc.apply_hybrid_promotion(
        weak, gemini_score=7, policy_mode="recovery", adapted_evaluation=adapted, enabled=True
    )
    assert balanced["promotion"] is False  # floor 8 in balanced
    assert recovery["promotion"] is True   # floor 7 in recovery
    assert recovery["hybrid_score_floor"] == lc.HYBRID_SCORE_FLOOR_RECOVERY


def test_hybrid_disabled_never_promotes_on_score_alone():
    lc = _load_module("services.lead_confidence", "services/lead_confidence.py")
    adapted = lc.adapt_gemini_evaluation_for_confidence({
        "score": 10,
        "confidence_level": "HIGH",
        "pain_point": "Urgent need",
        "company_name": "X",
    })
    weak = {"confidence_score": 10.0, "confidence_threshold": 62.0, "promotion": False, "reason": "weak-evidence fallback"}
    result = lc.apply_hybrid_promotion(
        weak, gemini_score=10, policy_mode="balanced", adapted_evaluation=adapted, enabled=False
    )
    assert result["promotion"] is False
    assert result["hybrid_promotion_triggered"] is False
    assert result["promotion_path"] == "none"


def test_hybrid_requires_basic_signals():
    lc = _load_module("services.lead_confidence", "services/lead_confidence.py")
    # score=8 but no pain, contact, company, tier, confidence_level
    adapted = {
        "score": 8,
        "tier": "LOW",
        "pain_summary": "",
        "contact_point": "",
        "company_name": "Unknown",
        "confidence_level": "",
    }
    weak = {"confidence_score": 10.0, "confidence_threshold": 62.0, "promotion": False, "reason": "weak-evidence fallback"}
    # has_basic_hybrid_signals: Unknown company_name is not meaningful; tier LOW fails.
    assert lc.has_basic_hybrid_signals(adapted, gemini_score=8) is False
    result = lc.apply_hybrid_promotion(
        weak, gemini_score=8, policy_mode="balanced", adapted_evaluation=adapted, enabled=True
    )
    assert result["promotion"] is False
    assert result["hybrid_eligible"] is False


def test_end_to_end_serper_dispatch_path_simulation():
    """Full adapter → confidence → hybrid flow for a typical Serper company-page lead."""
    lc = _load_module("services.lead_confidence", "services/lead_confidence.py")
    evaluation = {
        "score": 8,
        "confidence_level": "HIGH",
        "pain_point": "Fragmented brand narrative across product lines",
        "contact_endpoints": [{"platform": "linkedin", "uri": "https://linkedin.com/in/cmo"}],
        "company_name": "BrandCo",
        "matched_campaign_ids": ["camp1"],
        "dm": "Saw your messaging work...",
    }
    adapted = lc.adapt_gemini_evaluation_for_confidence(evaluation)
    conf = lc.calculate_lead_confidence(
        evaluation=adapted,
        text="BrandCo is a B2B industrial supplier with offices in Dubai and London.",
        url="https://brandco.example",
        source_tier="High",
        is_harvest_lead=False,
        is_thin_payload=False,
        threshold_adjustment=-1.2,  # mild domain leniency
    )
    final = lc.apply_hybrid_promotion(
        conf,
        gemini_score=8,
        policy_mode="recovery",
        is_thin_payload=False,
        adapted_evaluation=adapted,
    )
    assert final["promotion"] is True
    assert final["promotion_path"] in {"confidence", "hybrid_score"}
    assert adapted["_adapter_source"] == "gemini_score"
