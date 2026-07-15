import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from shared.campaign_enrichment import derive_campaign_enrichment


def test_enrichment_fills_sparse_real_estate_campaign_fields():
    campaign = {
        "name": "Oman Realty",
        "bio": "Product/Service: Oman Realty",
        "effective_bio": (
            "Premium real estate services in Oman covering residential villas, apartments, "
            "commercial spaces and land across Muscat, Salalah and Sohar."
        ),
        "keywords": (
            "property for sale Oman, villa Muscat, apartment rent Oman, commercial space Oman, "
            "property investment Oman"
        ),
        "location": "All, Oman, Asia",
        "geo_hierarchy": {"country": "Oman", "region": "All"},
        "pain_point": "Difficulty finding verified property listings in Oman, lack of pricing transparency",
        "sourcing_vector": "B2C",
        "intelligence_strategy": {
            "primary": "PLATFORM_MINING",
            "secondary": "COMPETITOR_TOUCHPOINT",
            "platform_targets": ["Property Finder Oman", "Bayut Oman", "OLX Oman", "Local real estate portals"],
            "competitor_names": [],
            "event_types": [],
            "vocabulary_notes": "",
            "decision_maker_titles": [],
        },
    }

    updates = derive_campaign_enrichment(campaign)

    assert updates["location"] == "Oman"
    assert "propertyfinder.com" in updates["intelligence_strategy"]["platform_targets"]
    assert "bayut.com" in updates["intelligence_strategy"]["platform_targets"]
    assert updates["persona_keywords"]
    assert any(signal.startswith("NOT ") for signal in updates["persona_targeting_signals"])
    assert updates["target_angle_hook"]
    assert updates["unfair_advantage"]
    assert updates["system_enrichment"]["query_style"] == "consumer"


def test_enrichment_respects_existing_user_authored_fields():
    campaign = {
        "name": "LeadGen Pro",
        "effective_bio": "Lead generation software for B2B sales teams",
        "keywords": "crm, sales automation, pipeline visibility",
        "location": "London, United Kingdom",
        "pain_point": "Hard to measure pipeline quality",
        "sourcing_vector": "B2B",
        "persona_keywords": "sales ops, revenue operations",
        "persona_targeting_signals": ["evaluating vendors"],
        "target_angle_hook": "Improve pipeline visibility quickly.",
        "unfair_advantage": "Faster setup with better attribution.",
        "intelligence_strategy": {
            "primary": "COLLOQUIAL_DISCOVERY",
            "secondary": "COMPETITOR_TOUCHPOINT",
            "platform_targets": ["G2", "Capterra"],
            "competitor_names": [],
            "event_types": [],
            "vocabulary_notes": "",
            "decision_maker_titles": [],
        },
    }

    updates = derive_campaign_enrichment(campaign)

    assert "persona_keywords" not in updates
    assert "persona_targeting_signals" not in updates
    assert "target_angle_hook" not in updates
    assert "unfair_advantage" not in updates
    assert "g2.com" in updates["intelligence_strategy"]["platform_targets"]
