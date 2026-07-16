import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "pipeline-main"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if PIPELINE_ROOT not in sys.path:
    sys.path.insert(0, PIPELINE_ROOT)

from services.domain_intelligence import (
    infer_domain_profile,
    apply_domain_query_profile,
    filter_tiered_urls_by_domain,
)


def test_infer_domain_profile_detects_real_estate():
    campaign = {
        "name": "Oman Realty Prospecting",
        "bio": "Help buyers find trusted property brokers for apartments and villas.",
        "keywords": "real estate, property agent, apartment, villa",
        "location": "Muscat, Oman",
    }
    profile = infer_domain_profile(campaign)
    assert profile["domain_family"] == "real_estate"
    assert profile["low_liquidity_market"] is True
    assert "frugal" in profile["blocked_subreddits"]


def test_domain_query_profile_drops_blocked_subreddits():
    profile = {
        "domain_family": "real_estate",
        "blocked_subreddits": ["frugal", "buyitforlife"],
        "preferred_query_hints": ["site:reddit.com/r/oman"],
    }
    queries = [
        "site:reddit.com/r/frugal looking for property in Oman",
        "site:reddit.com/r/Oman trusted property agent",
    ]
    result = apply_domain_query_profile(queries, profile)
    assert result["dropped"] == 1
    assert len(result["queries"]) == 1
    assert "/r/oman" in result["queries"][0].lower()


def test_domain_tier_filter_removes_blocked_reddit_urls():
    profile = {"blocked_subreddits": ["frugal"]}
    tiered = {
        "High": ["https://www.reddit.com/r/frugal/comments/a1/need_property_help/"],
        "Medium": ["https://www.reddit.com/r/Oman/comments/a2/property_agent/"],
        "Low": [],
    }
    result = filter_tiered_urls_by_domain(tiered, profile)
    assert result["dropped"] == 1
    assert len(result["tiered"]["High"]) == 0
    assert len(result["tiered"]["Medium"]) == 1

