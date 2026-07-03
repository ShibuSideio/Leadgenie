"""
Source Router — V25.1.0
=======================
Dynamically maps a campaign's ICP context and archetype to the correct
set of signal sources. Zero hardcoding of subreddits, keywords, or URLs.

DESIGN:
  1. Gemini analyzes the campaign's ICP context string (from context_builder)
     and returns a structured routing configuration.
  2. The router instantiates the appropriate BaseSignalSource objects.
  3. A deterministic fallback activates when Gemini times out or fails.

ARCHETYPE ROUTING:
  B2B    — Reddit (professional communities), HN (tech/startup), Stack Overflow,
            RSS (industry blogs), Job Posts (capability gap), Serper discovery
  B2C    — Reddit (consumer communities, expat forums), RSS (forums, portals),
            Google News RSS, Serper discovery (targeted forums)
  D2C    — Reddit (founder + consumer communities), HN (show_hn),
            RSS (product + entrepreneurship communities)
  B2B2C  — Job Posts (channel partner roles), RSS (trade publications),
            Serper discovery (partner/distributor forums)

All routing parameters (subreddits, search terms, feed URLs) are derived
from the campaign ICP — never preset per campaign_id or campaign_name.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from core.logging import get_logger                                           # type: ignore[import]
from services.gemini_service import call_gemini_2_5                           # type: ignore[import]
from services.signal_sources.base import BaseSignalSource                     # type: ignore[import]
from services.signal_sources.reddit import RedditSource                       # type: ignore[import]
from services.signal_sources.hackernews import HackerNewsSource               # type: ignore[import]
from services.signal_sources.rss_feed import RssFeedSource                    # type: ignore[import]
from services.signal_sources.serper_discovery import SerperDiscoverySource    # type: ignore[import]
from services.signal_sources.job_posts import JobPostSource                   # type: ignore[import]

log = get_logger("pipeline.source_router")

# ---------------------------------------------------------------------------
# Gemini output schema for routing configuration
# ---------------------------------------------------------------------------

_ROUTING_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reddit_sources": {
            "type": "ARRAY",
            "description": "Reddit subreddits and search queries to monitor. No r/ prefix needed.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "subreddit":    {"type": "STRING", "description": "Subreddit name without r/ prefix"},
                    "search_query": {"type": "STRING", "description": "Search query to run within this subreddit"},
                    "rationale":    {"type": "STRING", "description": "Why this subreddit is relevant for the ICP"},
                },
                "required": ["subreddit", "search_query", "rationale"],
            },
        },
        "hackernews_queries": {
            "type": "ARRAY",
            "description": "Hacker News search queries. Use for tech, SaaS, and startup ICPs.",
            "items": {"type": "STRING"},
        },
        "rss_feed_urls": {
            "type": "ARRAY",
            "description": "Full URLs of RSS/Atom feeds relevant to this ICP. Must be publicly accessible.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "url":      {"type": "STRING", "description": "Full RSS feed URL"},
                    "rationale": {"type": "STRING", "description": "Why this feed is relevant"},
                },
                "required": ["url", "rationale"],
            },
        },
        "job_post_keywords": {
            "type": "ARRAY",
            "description": "Job role titles/keywords that signal a buying trigger (capability gap). E.g. 'Head of Brand', 'VP Marketing'.",
            "items": {"type": "STRING"},
        },
        "serper_discovery_queries": {
            "type": "ARRAY",
            "description": "Google Search operator queries for Serper URL discovery. Use site: operators to target specific forums/communities. Full content retrieved by PRISM.",
            "items": {"type": "STRING"},
        },
        "geo_filter_terms": {
            "type": "ARRAY",
            "description": "Geographic terms to filter signals (city names, country names, region names). Leave empty for global/pan-geography campaigns.",
            "items": {"type": "STRING"},
        },
        "buyer_language_context": {
            "type": "STRING",
            "description": "Short summary of what language and signals to look for in this ICP context. Used as context for inline scoring.",
        },
    },
    "required": [
        "reddit_sources",
        "hackernews_queries",
        "rss_feed_urls",
        "job_post_keywords",
        "serper_discovery_queries",
        "geo_filter_terms",
        "buyer_language_context",
    ],
}

# ---------------------------------------------------------------------------
# Routing result
# ---------------------------------------------------------------------------

@dataclass
class RoutingConfig:
    """Fully instantiated source configuration for a campaign run."""
    sources: list[BaseSignalSource] = field(default_factory=list)
    buyer_language_context: str = ""
    geo_filter_terms: list[str] = field(default_factory=list)
    archetype: str = "B2B"
    derived_by: str = "gemini"  # "gemini" or "fallback"


# ---------------------------------------------------------------------------
# Source Router
# ---------------------------------------------------------------------------

class SourceRouter:
    """Maps a campaign's ICP context to an ordered list of signal sources.

    All routing decisions are made by Gemini based on the ICP context string.
    No hardcoded subreddits, no hardcoded topics — the LLM understands the
    campaign's ICP and selects appropriate sources dynamically.

    Args:
        serper_api_key: Serper API key for SerperDiscoverySource instances.
    """

    def __init__(self, serper_api_key: str = "") -> None:
        self._serper_key = serper_api_key

    def route(
        self,
        archetype: str,
        icp_context: str,
        geo: str,
        campaign: dict,
    ) -> RoutingConfig:
        """Derive signal sources for a campaign.

        Args:
            archetype:   Sourcing vector (B2B, B2C, D2C, B2B2C, etc.).
            icp_context: Assembled ICP context from context_builder.
            geo:         Campaign location string (may be empty).
            campaign:    Full campaign dict for additional field access.

        Returns:
            RoutingConfig with instantiated sources ready to call .discover().
        """
        log.info(
            "source_router_routing",
            archetype=archetype,
            geo=geo or "global",
            icp_chars=len(icp_context),
        )

        try:
            raw_config = self._derive_via_gemini(archetype, icp_context, geo)
            sources    = self._instantiate_sources(raw_config, geo)
            config     = RoutingConfig(
                sources                = sources,
                buyer_language_context = raw_config.get("buyer_language_context", ""),
                geo_filter_terms       = raw_config.get("geo_filter_terms", []),
                archetype              = archetype,
                derived_by             = "gemini",
            )
            log.info(
                "source_router_gemini_routing_complete",
                archetype=archetype,
                source_count=len(sources),
                source_types=[s.source_type for s in sources],
            )
            return config

        except Exception as exc:
            log.warning(
                "source_router_gemini_failed",
                archetype=archetype,
                error=str(exc),
                note="Falling back to heuristic routing.",
            )
            return self._fallback_routing(archetype, icp_context, geo, campaign)

    # ------------------------------------------------------------------
    # Gemini-driven routing
    # ------------------------------------------------------------------

    def _derive_via_gemini(
        self,
        archetype: str,
        icp_context: str,
        geo: str,
    ) -> dict:
        """Ask Gemini to analyze the ICP and output source configuration."""
        geo_note = f"Target geography: {geo}" if geo else "Geography: Global / not restricted."

        prompt = f"""You are an OSINT signal source router for a B2B/B2C/D2C intent lead generation platform.

TASK:
Given the following campaign ICP (Ideal Customer Profile), determine the best open-web signal
sources to monitor for ACTIVE BUYING INTENT. The goal is to find people or companies who are
RIGHT NOW expressing a need, pain, or active search that matches this ICP.

CAMPAIGN ARCHETYPE: {archetype}
{geo_note}

ICP CONTEXT:
{icp_context}

REQUIREMENTS:
1. Reddit subreddits: Select 3-6 subreddits where this ICP is likely to post about their pain.
   Include a search_query per subreddit that will surface pain/intent posts.
   Do NOT suggest subreddits that are clearly irrelevant.

2. Hacker News queries: Suggest 1-3 queries IF the ICP is a tech founder, engineer, SaaS buyer,
   or startup. Leave empty for pure consumer (B2C) or non-tech ICPs.

3. RSS feed URLs: Suggest 2-5 REAL, publicly accessible RSS feed URLs:
   - Google News RSS for news-triggered events (e.g. company announcements)
   - Industry forum RSS feeds
   - Niche community feeds
   Format: https://news.google.com/rss/search?q={'{search terms}'}&hl=en

4. Job post keywords: For B2B archetypes, suggest 2-4 job role titles that signal a BUYING
   TRIGGER (the company needs to hire because they lack this capability = they need a vendor).
   Leave empty for B2C archetypes.

5. Serper discovery queries: Suggest 2-4 Google Search operator queries to find relevant
   forum threads and community discussions that aren't on Reddit.
   Use site: operators: e.g. "site:expatriates.com Oman property rent"

6. Geo filter terms: If geography matters, list the geographic terms to filter signals.
   These are used to ensure signals are relevant to the target location.
   Leave empty if the ICP is global or geography-independent.

7. Buyer language context: Describe in 2-3 sentences what language patterns indicate a
   HIGH-INTENT buyer for this ICP. What EXACT WORDS would they use?

IMPORTANT:
- ALL suggestions must be based on the ICP above — nothing generic
- Subreddit names must be real subreddits that exist
- RSS URLs must be properly formatted and publicly accessible
- Do not suggest subreddits/sources that are clearly off-topic
- For geo-specific ICPs (e.g. Oman real estate), focus sources on that geography"""

        result = call_gemini_2_5(
            prompt,
            expect_json=True,
            response_schema=_ROUTING_SCHEMA,
        )
        if not isinstance(result, dict):
            raise ValueError(f"Routing schema returned unexpected type: {type(result)}")
        return result

    def _instantiate_sources(self, config: dict, geo: str) -> list[BaseSignalSource]:
        """Convert Gemini routing config into BaseSignalSource instances."""
        sources: list[BaseSignalSource] = []
        geo_filter_terms = config.get("geo_filter_terms", [])

        # 1. Reddit sources
        reddit_configs = config.get("reddit_sources", [])
        if reddit_configs:
            subreddits   = [r.get("subreddit", "") for r in reddit_configs if r.get("subreddit")]
            search_terms = list({r.get("search_query", "") for r in reddit_configs if r.get("search_query")})
            if subreddits:
                sources.append(RedditSource(
                    subreddits   = subreddits,
                    search_terms = search_terms,
                    geo_terms    = geo_filter_terms if geo_filter_terms else None,
                    max_age_days = 14,
                ))
                log.info(
                    "source_router_reddit_configured",
                    subreddits=subreddits,
                    search_terms=search_terms[:3],
                )

        # 2. Hacker News
        hn_queries = [q for q in config.get("hackernews_queries", []) if q]
        if hn_queries:
            sources.append(HackerNewsSource(
                search_queries  = hn_queries,
                max_age_days    = 30,
                include_ask     = True,
                include_stories = True,
                min_comments    = 2,
            ))

        # 3. RSS feeds
        rss_configs = config.get("rss_feed_urls", [])
        rss_urls    = [r.get("url", "") for r in rss_configs if r.get("url")]
        if rss_urls:
            keyword_filters = self._extract_keyword_filters(config)
            sources.append(RssFeedSource(
                feed_urls       = rss_urls,
                keyword_filters = keyword_filters,
                geo_terms       = geo_filter_terms if geo_filter_terms else None,
                max_age_days    = 14,
            ))

        # 4. Job posts (B2B signal only)
        job_keywords = [k for k in config.get("job_post_keywords", []) if k]
        if job_keywords:
            sources.append(JobPostSource(
                role_keywords = job_keywords,
                geo           = geo,
                max_age_days  = 30,
            ))

        # 5. Serper discovery (URLs only → PRISM scrapes for full content)
        discovery_queries = [q for q in config.get("serper_discovery_queries", []) if q]
        if discovery_queries and self._serper_key:
            # Derive geo code from geo string (e.g. "Oman" → "om", "India" → "in")
            # Do NOT map — pass empty geo_code; queries already contain geo-specificity
            sources.append(SerperDiscoverySource(
                discovery_queries = discovery_queries,
                serper_api_key    = self._serper_key,
                num_results       = 10,
                geo_code          = "",  # No geo restriction — queries are already targeted
            ))

        return sources

    def _extract_keyword_filters(self, config: dict) -> list[str]:
        """Extract keyword hints from Gemini routing config for RSS filtering."""
        # Derive from reddit search terms as keyword hints for RSS filtering
        keywords: list[str] = []
        for reddit_cfg in config.get("reddit_sources", []):
            query = reddit_cfg.get("search_query", "")
            # Extract meaningful words (>4 chars, skip operators)
            words = [w.strip('"\'()') for w in query.split() if len(w) > 4]
            keywords.extend(words[:3])
        return list(set(keywords))[:10]

    # ------------------------------------------------------------------
    # Deterministic fallback routing (when Gemini fails)
    # ------------------------------------------------------------------

    def _fallback_routing(
        self,
        archetype: str,
        icp_context: str,
        geo: str,
        campaign: dict,
    ) -> RoutingConfig:
        """Heuristic routing when Gemini is unavailable.

        Derives search terms from the campaign's keywords field.
        Selects subreddits by archetype category.
        """
        log.info(
            "source_router_using_fallback",
            archetype=archetype,
            geo=geo or "global",
        )

        # Extract search terms from ICP context (first 10 meaningful words)
        keywords_raw = campaign.get("keywords", "") or campaign.get("bio", "")
        search_terms = [
            w.strip("\"',.:;()").lower()
            for w in (keywords_raw or "").split()
            if len(w.strip("\"',.:;()")) > 4
        ][:6]

        sources: list[BaseSignalSource] = []
        norm_arch = archetype.upper()

        # Archetype → generic subreddit fallback set
        if "B2C" in norm_arch:
            subreddits = ["expats", "personalfinance", "travel", "askreddit"]
        elif "D2C" in norm_arch:
            subreddits = ["Entrepreneur", "ecommerce", "smallbusiness", "startups"]
        elif "B2B2C" in norm_arch:
            subreddits = ["startups", "Entrepreneur", "sales", "smallbusiness"]
        else:  # B2B default
            subreddits = ["marketing", "startups", "Entrepreneur", "SaaS", "sales"]

        if subreddits and search_terms:
            sources.append(RedditSource(
                subreddits   = subreddits,
                search_terms = search_terms,
                geo_terms    = [geo] if geo else None,
                max_age_days = 14,
            ))

        # Always add Google News RSS as a fallback discovery source
        if search_terms:
            from urllib.parse import quote_plus
            gnews_query = " ".join(search_terms[:4])
            gnews_url   = f"https://news.google.com/rss/search?q={quote_plus(gnews_query)}&hl=en"
            sources.append(RssFeedSource(
                feed_urls    = [gnews_url],
                max_age_days = 7,
            ))

        # B2B fallback: HN
        if "B2B" in norm_arch or "SAAS" in norm_arch:
            sources.append(HackerNewsSource(
                search_queries = search_terms[:2] if search_terms else ["startup problems"],
                max_age_days   = 30,
                include_ask    = True,
                include_stories= False,
                min_comments   = 3,
            ))

        return RoutingConfig(
            sources                = sources,
            buyer_language_context = "",
            geo_filter_terms       = [geo] if geo else [],
            archetype              = archetype,
            derived_by             = "fallback",
        )
