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
