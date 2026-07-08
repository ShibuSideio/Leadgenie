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

core_pkg = types.ModuleType("core")
core_pkg.__path__ = [str(Path(PIPELINE_ROOT) / "core")]
sys.modules["core"] = core_pkg


def _load_module(module_name: str, relative_path: str):
    module_path = Path(PIPELINE_ROOT) / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_inline_score_signal_falls_back_to_heuristics_on_llm_error(monkeypatch):
    gemini_service = _load_module("services.gemini_service", "services/gemini_service.py")

    def _raise(*args, **kwargs):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(gemini_service, "call_gemini_2_5", _raise)
    result = gemini_service.inline_score_signal(
        signal_text="Looking for a CRM because our lead scoring is broken and we need a fix urgently",
        icp_context="B2B SaaS for sales teams",
        source_url="https://reddit.com/r/sales/comments/1",
        source_type="reddit",
        geo_target="US",
        archetype="B2B",
        buyer_language_context="",
    )

    assert result["tier"] == "HIGH"
    assert result["topic_coherence"] >= 0.6


def test_budget_guard_blocks_spending_after_daily_limit(tmp_path):
    budget_guard = _load_module("services.budget_guard", "services/budget_guard.py")
    state_path = tmp_path / "budget.json"
    guard = budget_guard.BudgetGuard(daily_limit=1, state_path=str(state_path))

    assert guard.can_spend(1)
    guard.record_spend(1)
    assert not guard.can_spend(1)


def test_cluster_analyst_uses_heuristic_fallback_when_llm_fails(monkeypatch):
    cluster_analyst = _load_module("services.signal_cluster_analyst", "services/signal_cluster_analyst.py")

    def _raise(*args, **kwargs):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(cluster_analyst, "call_gemini_2_5", _raise)
    clusters = cluster_analyst._gemini_cluster(
        [
            {"snippet_text": "Looking for a CRM because our lead scoring is broken", "harvested_at": "2026-07-08T00:00:00Z"},
            {"snippet_text": "Need help with sales automation", "harvested_at": "2026-07-08T01:00:00Z"},
        ],
        "B2B SaaS sales teams",
        "B2B",
        "US",
    )

    assert clusters
    assert clusters[0]["cluster_label"] == "public-intent fallback cluster"
