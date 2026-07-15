import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ORCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if ORCH_ROOT not in sys.path:
    sys.path.insert(0, ORCH_ROOT)

from jobs.campaign_enrichment_job import _campaign_needs_enrichment


def test_campaign_needs_enrichment_when_operational_fields_missing():
    campaign = {
        "location": "All, Oman, Asia",
        "persona_keywords": "",
        "persona_targeting_signals": [],
        "target_angle_hook": "",
        "unfair_advantage": "",
        "intelligence_strategy": {"platform_targets": ["Property Finder Oman"]},
        "system_enrichment": {},
    }

    assert _campaign_needs_enrichment(campaign) is True


def test_campaign_does_not_need_enrichment_when_current_and_complete():
    campaign = {
        "location": "Muscat, Oman",
        "persona_keywords": "verified property listings, buy property oman",
        "persona_targeting_signals": ["looking to buy property", "NOT jobs"],
        "target_angle_hook": "Find verified property options faster.",
        "unfair_advantage": "Verified listings with transparent pricing.",
        "intelligence_strategy": {"platform_targets": ["propertyfinder.com", "bayut.com"]},
        "system_enrichment": {"enrichment_version": "2026-07-15-auto-enrichment-v1"},
        "_query_exhaustion_consecutive_zeros": 0,
    }

    assert _campaign_needs_enrichment(campaign) is False
