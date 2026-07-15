import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if PIPELINE_ROOT not in sys.path:
    sys.path.insert(0, PIPELINE_ROOT)

from services.query_governance import (
    govern_query_portfolio,
    filter_queries_against_memory,
    build_exhaustion_escalation_queries,
    query_signature,
)


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


def test_query_memory_filter_drops_seen_queries():
    queries = [
        '"trusted property agents oman"',
        '"muscat broker expensive"',
        '"buy apartment Muscat direct owner"',
    ]
    prior = [query_signature('"muscat broker expensive"')]
    result = filter_queries_against_memory(queries, prior_signatures=prior, keep_minimum=2)
    assert result["dropped"] == 1
    assert '"muscat broker expensive"' not in result["queries"]
    assert len(result["queries"]) == 2


def test_query_memory_filter_keeps_minimum_when_all_seen():
    queries = ['"trusted property agents oman"', '"muscat broker expensive"']
    prior = [query_signature(q) for q in queries]
    result = filter_queries_against_memory(queries, prior_signatures=prior, keep_minimum=2)
    assert result["queries"] == queries


def test_exhaustion_escalation_queries_expand_with_level():
    campaign = {
        "keywords": "property for sale Oman",
        "intelligence_strategy": {
            "primary": "PLATFORM_MINING",
            "platform_targets": ["Property Finder Oman", "Bayut Oman"],
        },
    }
    level_1 = build_exhaustion_escalation_queries(campaign, location="Muscat, Oman", level=1)
    level_3 = build_exhaustion_escalation_queries(campaign, location="Muscat, Oman", level=3)
    assert len(level_1) >= 3
    assert len(level_3) >= len(level_1)
    assert any("site:propertyfinder.com" in q for q in level_1)
