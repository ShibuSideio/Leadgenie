"""V27.0.1 — Vertex AI project resolution + platform-mining fail-open."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main")
)
for path in (ROOT, PIPELINE_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from core.config import (  # noqa: E402
    resolve_vertex_ai_location,
    resolve_vertex_ai_project,
)
from services.query_brain import (  # noqa: E402
    _generate_platform_mining_queries,
    _platform_mining_deterministic_fallback,
)


def test_vertex_project_prefers_vertex_ai_project_env(monkeypatch):
    monkeypatch.setenv("VERTEX_AI_PROJECT", "lead-sniper-prod")
    monkeypatch.setenv("PROJECT_ID", "sideio-leads-v16")
    assert resolve_vertex_ai_project() == "lead-sniper-prod"


def test_vertex_project_falls_back_to_project_id(monkeypatch):
    monkeypatch.delenv("VERTEX_AI_PROJECT", raising=False)
    monkeypatch.setenv("PROJECT_ID", "sideio-leads-v16")
    assert resolve_vertex_ai_project() == "sideio-leads-v16"


def test_vertex_project_default_is_lead_sniper_not_trendpulse(monkeypatch):
    monkeypatch.delenv("VERTEX_AI_PROJECT", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)
    project = resolve_vertex_ai_project()
    assert project == "lead-sniper-prod"
    assert project != "trendpulse-app-2025"
    assert "trendpulse" not in project


def test_vertex_location_resolution(monkeypatch):
    monkeypatch.setenv("VERTEX_AI_LOCATION", "us-central1")
    monkeypatch.setenv("LOCATION", "asia-south1")
    assert resolve_vertex_ai_location() == "us-central1"
    monkeypatch.delenv("VERTEX_AI_LOCATION", raising=False)
    assert resolve_vertex_ai_location() == "asia-south1"


def test_init_vertex_never_uses_trendpulse_default(monkeypatch):
    """init_vertex must resolve project without trendpulse hardcode."""
    monkeypatch.delenv("VERTEX_AI_PROJECT", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)

    import core.clients as clients

    # Reset singleton so init re-runs
    monkeypatch.setattr(clients, "_vertex_initialised", False)
    monkeypatch.setattr(clients, "_vertex_project_used", None)

    captured: dict = {}

    def _fake_init(project=None, location=None, **kwargs):
        captured["project"] = project
        captured["location"] = location

    monkeypatch.setattr(clients.vertexai, "init", _fake_init)
    clients.init_vertex()
    assert captured.get("project") == "lead-sniper-prod"
    assert captured.get("project") != "trendpulse-app-2025"
    assert clients.get_vertex_project() == "lead-sniper-prod"


def test_platform_mining_gemini_403_uses_deterministic_fallback(monkeypatch):
    """403 from Vertex must not raise; returns rule-based site: queries."""
    ctx = SimpleNamespace(campaign_id="camp-test-403")
    domain_profile = {
        "domain_family": "real_estate",
        "preferred_query_hints": ["site:bayut.com", "site:propertyfinder.com"],
    }

    def _raise_403(*_a, **_k):
        raise Exception(
            "403 Permission denied on resource project trendpulse-app-2025 "
            "for model gemini-2.5-flash"
        )

    with patch(
        "services.gemini_service.call_gemini_2_5",
        side_effect=_raise_403,
    ):
        # Patch the import path used inside the function
        with patch(
            "services.query_brain.call_gemini_2_5",
            side_effect=_raise_403,
            create=True,
        ):
            # The function imports call_gemini_2_5 inside the body
            import services.gemini_service as gemini_mod

            monkeypatch.setattr(gemini_mod, "call_gemini_2_5", _raise_403)
            queries = _generate_platform_mining_queries(
                ctx=ctx,
                bio="Property agents in Muscat Oman",
                kw_str="villa, apartment, agent",
                vector_label="B2C",
                blacklist="",
                strategy_plan={
                    "platform_targets": ["bayut.com", "propertyfinder.com"],
                    "geo_terms": ["Muscat", "Oman"],
                },
                domain_profile=domain_profile,
            )

    assert isinstance(queries, list)
    assert len(queries) >= 1
    assert all("site:" in q for q in queries)
    assert any("bayut.com" in q or "propertyfinder.com" in q for q in queries)


def test_deterministic_fallback_family_defaults_when_no_targets():
    queries = _platform_mining_deterministic_fallback(
        campaign_id="c1",
        strategy_plan=None,
        domain_profile={"domain_family": "saas"},
        domain_targets=[],
        domain_family="saas",
        reason="gemini_403",
    )
    assert len(queries) >= 1
    assert any("g2.com" in q or "capterra.com" in q for q in queries)


def test_deterministic_fallback_last_resort_channels():
    queries = _platform_mining_deterministic_fallback(
        campaign_id="c2",
        strategy_plan=None,
        domain_profile=None,
        domain_targets=[],
        domain_family="unknown_vertical",
        reason="gemini_error:TimeoutError",
    )
    assert len(queries) >= 1
    assert all("site:" in q for q in queries)
