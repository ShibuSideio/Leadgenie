"""Shared intelligence profile inference for autonomous campaign strategy.

This module provides a deterministic, low-input intelligence profile for a
campaign so the backend can make stronger strategic decisions even when users
supply very sparse data.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List


def infer_campaign_intelligence_profile(
    effective_bio: str = "",
    keywords: str = "",
    location: str = "",
    campaign_name: str = "",
    pain_point: str = "",
    sourcing_vector: str = "",
) -> Dict[str, Any]:
    """Infer a robust intelligence profile from sparse campaign inputs.

    The goal is to minimize user effort while maximizing backend intelligence.
    The profile is deterministic and does not require Gemini for the common case.
    """
    text = " ".join(
        [effective_bio or "", keywords or "", campaign_name or "", pain_point or ""]
    ).lower()

    def _contains_any(*terms: str) -> bool:
        return any(term in text for term in terms)

    # Heuristic archetype detection
    if sourcing_vector:
        vector = sourcing_vector.upper().strip()
    elif _contains_any("real estate", "property", "villa", "apartment", "broker", "agent", "listing"):
        vector = "B2C"
    elif _contains_any("saas", "software", "b2b", "enterprise", "crm", "lead generation", "marketing"):
        vector = "B2B"
    elif _contains_any("ecommerce", "shop", "retail", "consumer", "subscription", "direct to consumer"):
        vector = "D2C"
    else:
        vector = "B2B"

    platform_targets: List[str] = []
    competitor_names: List[str] = []
    primary_strategy = "COLLOQUIAL_DISCOVERY"
    secondary_strategy = "NONE"
    vocabulary_notes = ""
    decision_maker_titles: List[str] = []
    event_types: List[str] = []

    if vector == "B2C":
        primary_strategy = "PLATFORM_MINING"
        secondary_strategy = "COLLOQUIAL_DISCOVERY"
        if _contains_any("real estate", "property", "villa", "apartment"):
            platform_targets = ["propertyfinder.com", "bayut.com", "dubizzle.com", "zoopla.co.uk"]
            competitor_names = ["Property Finder", "Bayut", "Dubizzle"]
            vocabulary_notes = (
                "The buyer uses local, practical language such as 'villa', 'apartment', "
                "'near me', 'family home', 'budget', and 'move in'."
            )
        else:
            platform_targets = ["yelp.com", "google.com", "trustpilot.com"]
            competitor_names = ["Local competitors"]
            vocabulary_notes = "The buyer uses everyday consumer language rather than professional jargon."
    elif vector == "D2C":
        primary_strategy = "COLLOQUIAL_DISCOVERY"
        secondary_strategy = "COMPETITOR_TOUCHPOINT"
        platform_targets = ["amazon.com", "shopify.com", "yelp.com"]
        competitor_names = ["Direct competitors"]
        vocabulary_notes = "The buyer discusses product value, shipping, refunds, and usage in casual terms."
    elif vector == "B2B2C":
        primary_strategy = "COLLOQUIAL_DISCOVERY"
        secondary_strategy = "PROFESSIONAL_NETWORK"
        platform_targets = ["linkedin.com", "industryforums.com"]
        decision_maker_titles = ["Operations Manager", "Director", "VP"]
        vocabulary_notes = "The buyer often uses role-based language and operational pain."
    else:
        primary_strategy = "COLLOQUIAL_DISCOVERY"
        secondary_strategy = "COMPETITOR_TOUCHPOINT"
        platform_targets = ["g2.com", "capterra.com", "trustpilot.com"]
        competitor_names = ["Comparable software providers"]
        decision_maker_titles = ["VP Operations", "Head of Sales", "CTO"]
        vocabulary_notes = "The buyer uses professional terms tied to workflow, ROI, and implementation."

    if _contains_any("funding", "raised", "hiring", "expansion", "regulation", "breach"):
        event_types = ["funding", "expansion", "regulatory"]

    # Add geo hints from location for stronger downstream routing
    geo_terms: List[str] = []
    if location:
        geo_terms.extend(re.findall(r"[A-Za-z]+", location))

    return {
        "sourcing_vector": vector,
        "primary_strategy": primary_strategy,
        "secondary_strategy": secondary_strategy,
        "platform_targets": platform_targets,
        "competitor_names": competitor_names,
        "event_types": event_types,
        "vocabulary_notes": vocabulary_notes or (
            "The buyer uses practical, everyday language related to the product and local context."
        ),
        "decision_maker_titles": decision_maker_titles,
        "geo_terms": geo_terms[:5],
        "inferred_from": "heuristics",
    }


def build_intelligence_strategy_plan(campaign: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a campaign profile into a concrete execution plan.

    This gives downstream services one unified plan that can drive source
    prioritization, query style, and platform-targeting decisions.
    """
    profile = infer_campaign_intelligence_profile(
        effective_bio=campaign.get("effective_bio") or campaign.get("bio") or "",
        keywords=campaign.get("keywords") or "",
        location=campaign.get("location") or "",
        campaign_name=campaign.get("name") or "",
        pain_point=campaign.get("pain_point") or "",
        sourcing_vector=campaign.get("sourcing_vector") or "",
    )

    source_priorities: List[str] = []
    query_style = "business"
    if profile["sourcing_vector"] == "B2C":
        source_priorities = ["classified_listings", "consumer_forums", "reddit", "serper_discovery"]
        query_style = "consumer"
    elif profile["sourcing_vector"] == "D2C":
        source_priorities = ["consumer_forums", "reddit", "serper_discovery", "youtube"]
        query_style = "consumer"
    elif profile["sourcing_vector"] == "B2B2C":
        source_priorities = ["reddit", "serper_discovery", "job_posts", "rss_feed"]
        query_style = "hybrid"
    else:
        source_priorities = ["reddit", "serper_discovery", "google_reviews", "rss_feed"]
        query_style = "business"

    return {
        "sourcing_vector": profile["sourcing_vector"],
        "primary_strategy": profile["primary_strategy"],
        "secondary_strategy": profile["secondary_strategy"],
        "platform_targets": profile["platform_targets"],
        "competitor_names": profile["competitor_names"],
        "vocabulary_notes": profile["vocabulary_notes"],
        "source_priorities": source_priorities,
        "query_style": query_style,
        "geo_terms": profile["geo_terms"],
    }
