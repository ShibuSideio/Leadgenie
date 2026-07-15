import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if PIPELINE_ROOT not in sys.path:
    sys.path.insert(0, PIPELINE_ROOT)

from services.query_governance import govern_query_portfolio


def test_governance_caps_negative_intent_ratio():
    campaign = {
        "intelligence_strategy": {
            "primary": "COLLOQUIAL_DISCOVERY",
            "platform_targets": [],
        }
    }
    candidates = [
        "Oman property hidden fees -site:a.com -site:b.com -site:c.com -site:d.com -site:e.com -site:f.com -site:g.com",
        "Oman property fake listings -site:a.com -site:b.com -site:c.com -site:d.com -site:e.com -site:f.com -site:g.com",
        "Oman property agent scam -site:a.com -site:b.com -site:c.com -site:d.com -site:e.com -site:f.com -site:g.com",
        'site:reddit.com/r/Oman "property agent Oman"',
        'site:trustpilot.com "Oman real estate broker"',
        '"verified property listings Oman"',
        '"buy apartment Muscat direct owner"',
    ]

    result = govern_query_portfolio(candidates, campaign=campaign, sourcing_vector="B2C", location="Muscat, Oman")

    governed = result["queries"]
    negative_count = sum(
        1 for q in governed if any(term in q.lower() for term in ("scam", "fake", "hidden fees"))
    )
    assert len(governed) >= 5
    assert negative_count <= 2
    assert result["stats"]["blacklist_sites_trimmed"] > 0


def test_platform_mining_injects_site_queries_when_missing():
    campaign = {
        "keywords": "property for sale Oman, villa Muscat",
        "intelligence_strategy": {
            "primary": "PLATFORM_MINING",
            "platform_targets": ["Property Finder Oman", "Bayut Oman", "OLX Oman"],
        },
    }
    candidates = [
        '"trusted property agents oman"',
        '"muscat broker expensive"',
        '"oman property misleading listings"',
    ]

    result = govern_query_portfolio(candidates, campaign=campaign, sourcing_vector="B2C", location="Muscat, Oman")

    governed = result["queries"]
    assert any("site:propertyfinder.com" in q for q in governed)
    assert any("site:bayut.com" in q for q in governed)
    assert result["stats"]["platform_injected"] >= 1
