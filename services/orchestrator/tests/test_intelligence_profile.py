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

from shared.intelligence_profile import (
    build_intelligence_strategy_plan,
    infer_campaign_intelligence_profile,
)

_PIPELINE_SERVICES_PATH = Path(PIPELINE_ROOT) / "services"
_PIPELINE_CORE_PATH = Path(PIPELINE_ROOT) / "core"

services_pkg = types.ModuleType("services")
services_pkg.__path__ = [str(_PIPELINE_SERVICES_PATH)]
sys.modules["services"] = services_pkg

core_pkg = types.ModuleType("core")
core_pkg.__path__ = [str(_PIPELINE_CORE_PATH)]
sys.modules["core"] = core_pkg

_MODULE_PATH = _PIPELINE_SERVICES_PATH / "signal_cluster_analyst.py"
_SPEC = importlib.util.spec_from_file_location("services.signal_cluster_analyst", _MODULE_PATH)
_CLUSTER_ANALYST = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CLUSTER_ANALYST
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_CLUSTER_ANALYST)
_score_cluster = _CLUSTER_ANALYST._score_cluster


def test_real_estate_campaign_defaults_to_consumer_profile():
    profile = infer_campaign_intelligence_profile(
        effective_bio="We help clients find villas and apartments in Muscat",
        keywords="real estate, property, oman",
        location="Muscat, Oman",
        campaign_name="Oman Realty",
    )

    assert profile["sourcing_vector"] == "B2C"
    assert profile["primary_strategy"] == "PLATFORM_MINING"
    assert any("propertyfinder" in target for target in profile["platform_targets"])


def test_saas_campaign_defaults_to_b2b_profile():
    profile = infer_campaign_intelligence_profile(
        effective_bio="B2B SaaS platform for sales teams",
        keywords="crm, sales automation, lead generation",
        location="Global",
        campaign_name="LeadGen Pro",
    )

    assert profile["sourcing_vector"] == "B2B"
    assert profile["primary_strategy"] in {"COLLOQUIAL_DISCOVERY", "COMPETITOR_TOUCHPOINT"}
    assert profile["decision_maker_titles"]


def test_strategy_plan_prioritizes_consumer_sources_for_real_estate():
    plan = build_intelligence_strategy_plan(
        {
            "name": "Oman Realty",
            "effective_bio": "We help clients find villas and apartments in Muscat",
            "keywords": "real estate, property, oman",
            "location": "Muscat, Oman",
        }
    )

    assert plan["query_style"] == "consumer"
    assert plan["source_priorities"][0] == "classified_listings"
    assert any("propertyfinder" in target for target in plan["platform_targets"])


def test_cluster_scoring_receives_strategy_boost_for_matching_signals():
    cluster = {"contributing_indices": [0, 1]}
    signals = [
        {"source_type": "classified_listings", "snippet_text": "Luxury villa near Muscat for sale", "harvested_at": "2026-07-08T00:00:00Z"},
        {"source_type": "consumer_forums", "snippet_text": "Looking for apartment with family home budget", "harvested_at": "2026-07-08T01:00:00Z"},
    ]
    strategy_plan = {
        "sourcing_vector": "B2C",
        "source_priorities": ["classified_listings", "consumer_forums", "reddit"],
    }

    score = _score_cluster(cluster, signals, strategy_plan=strategy_plan)

    assert score > 60
