"""
Source Router — V25.2.0
=======================
Dynamically maps a campaign's ICP context and archetype to the correct
set of signal sources. Zero hardcoding of subreddits, keywords, or URLs.

DESIGN:
  1. Gemini analyzes the campaign's ICP context string (from context_builder)
     and returns a structured routing configuration — with separate instruction
     tracks per archetype (B2B, B2C, D2C, B2B2C).
  2. The router instantiates the appropriate BaseSignalSource objects.
  3. A deterministic fallback activates when Gemini times out or fails.

ARCHETYPE ROUTING:
  B2B    — Reddit (professional communities), HN (tech/startup),
            RSS (industry blogs), Job Posts (capability gap), Serper discovery

  B2C    — ClassifiedListings (expat forums, property portals, want ads),
            ConsumerForum (review platforms, product comparisons),
            Reddit (consumer intent subreddits),
            RSS (portal feeds, Google News consumer events),
            Serper discovery (site: operators on consumer platforms)

  D2C    — ConsumerForum (product discovery + D2C founder spaces),
            Reddit (r/ecommerce, r/Entrepreneur, r/smallbusiness),
            HN Show HN (product launches and D2C founder problems),
            RSS (IndieHackers, ProductHunt),
            Serper discovery (competitor + switching intent)

  B2B2C  — Job Posts (channel partner roles), RSS (trade publications),
            Serper discovery (partner/distributor forums),
            Reddit (industry + end-consumer communities)

All routing parameters are derived from the campaign ICP — never hardcoded.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from core.logging import get_logger                                           # type: ignore[import]
from services.gemini_service import call_gemini_2_5                           # type: ignore[import]
from services.signal_sources.base import BaseSignalSource                     # type: ignore[import]
from services.signal_sources.reddit import RedditSource                           # type: ignore[import]
from services.signal_sources.hackernews import HackerNewsSource                   # type: ignore[import]
from services.signal_sources.rss_feed import RssFeedSource                        # type: ignore[import]
from services.signal_sources.serper_discovery import SerperDiscoverySource        # type: ignore[import]
from services.signal_sources.job_posts import JobPostSource                       # type: ignore[import]
from services.signal_sources.classified_listings import ClassifiedListingSource   # type: ignore[import]
from services.signal_sources.consumer_forum import ConsumerForumSource            # type: ignore[import]
from services.signal_sources.google_reviews import GoogleReviewSource             # type: ignore[import]
from services.signal_sources.youtube import YouTubeSource                         # type: ignore[import]

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
            "description": "Hacker News search queries. Use ONLY for tech, SaaS, startup, or D2C founder ICPs. Leave empty for B2C consumer ICPs.",
            "items": {"type": "STRING"},
        },
        "rss_feed_urls": {
            "type": "ARRAY",
            "description": "Full URLs of RSS/Atom feeds relevant to this ICP. Must be publicly accessible. For B2C: include consumer portals, expat forums, classified feeds. For B2B: include industry blogs and trade publications.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "url":       {"type": "STRING", "description": "Full RSS feed URL"},
                    "rationale": {"type": "STRING", "description": "Why this feed is relevant"},
                },
                "required": ["url", "rationale"],
            },
        },
        "job_post_keywords": {
            "type": "ARRAY",
            "description": "Job role titles that signal a buying trigger. Use ONLY for B2B/B2B2C archetypes. Leave empty for B2C/D2C pure consumer ICPs.",
            "items": {"type": "STRING"},
        },
        "serper_discovery_queries": {
            "type": "ARRAY",
            "description": "Google Search operator queries for Serper URL discovery. For B2C/D2C: use site: operators to target consumer platforms (expatriates.com, dubizzle.com, reddit.com, quora.com, tripadvisor.com). For B2B: target professional forums and industry sites. Full content retrieved by PRISM.",
            "items": {"type": "STRING"},
        },
        "geo_filter_terms": {
            "type": "ARRAY",
            "description": "Geographic terms to filter signals (city names, country names, region names). Leave empty for global/pan-geography campaigns.",
            "items": {"type": "STRING"},
        },
        "buyer_language_context": {
            "type": "STRING",
            "description": "Short summary of what language and signals to look for in this ICP context. What EXACT WORDS would a high-intent buyer use?",
        },
        # B2C/D2C specific fields
        "classified_listing_config": {
            "type": "OBJECT",
            "description": "Configuration for classified ad / consumer portal discovery. Relevant for B2C and D2C archetypes. Leave empty for B2B.",
            "properties": {
                "categories": {
                    "type": "ARRAY",
                    "description": "Product/service category terms that buyers search on classified sites (e.g. ['villa', 'apartment', 'office space'])",
                    "items": {"type": "STRING"},
                },
                "platform_types": {
                    "type": "ARRAY",
                    "description": "Platform types to search. Options: 'property', 'expat', 'classified', 'news'. Select those relevant to the ICP.",
                    "items": {"type": "STRING"},
                },
            },
            "required": [],
        },
        "consumer_forum_config": {
            "type": "OBJECT",
            "description": "Configuration for consumer review and product forum discovery. Relevant for B2C and D2C archetypes.",
            "properties": {
                "product_category": {
                    "type": "STRING",
                    "description": "Primary product or service category for consumer forum search (e.g. 'interior design', 'running shoes', 'meal delivery')",
                },
                "include_d2c_founders": {
                    "type": "BOOLEAN",
                    "description": "True for D2C archetype — also monitors founder communities (IndieHackers, Show HN, ProductHunt)",
                },
            },
            "required": [],
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
        "classified_listing_config",
        "consumer_forum_config",
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
            raw_config = self._derive_via_gemini(archetype, icp_context, geo, campaign=campaign)
            sources    = self._instantiate_sources(raw_config, geo, campaign=campaign)
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
        campaign: dict | None = None,
    ) -> dict:
        """Ask Gemini to analyze the ICP and output source configuration."""
        geo_note = f"Target geography: {geo}" if geo else "Geography: Global / not restricted."

        # Build archetype-specific instruction block
        archetype_instructions = _build_archetype_instructions(archetype)

        # V26: Prepend strategy-specific instructions if intelligence_strategy is present
        strategy_instructions = ""
        if campaign:
            _intel_strategy = campaign.get("intelligence_strategy")
            if isinstance(_intel_strategy, dict) and _intel_strategy.get("primary"):
                strategy_instructions = _build_strategy_instructions(
                    _intel_strategy.get("primary", ""),
                    campaign,
                )
                if strategy_instructions:
                    archetype_instructions = (
                        strategy_instructions + "\n\n"
                        "--- ARCHETYPE CONTEXT (secondary) ---\n"
                        + archetype_instructions
                    )

        prompt = f"""You are an OSINT signal source router for a B2B/B2C/D2C/B2B2C intent lead generation platform.

TASK:
Given the following campaign ICP, determine the BEST OPEN-WEB SIGNAL SOURCES for ACTIVE BUYING INTENT.
The goal: find people or organizations RIGHT NOW expressing a need that matches this ICP.

CAMPAIGN ARCHETYPE: {archetype}
{geo_note}

ICP CONTEXT:
{icp_context}

--- ARCHETYPE-SPECIFIC INSTRUCTIONS ---
{archetype_instructions}

--- UNIVERSAL REQUIREMENTS ---
A. Reddit: Select 3-6 subreddits where this ICP posts about their pain.
   Match subreddits to BUYER type — not seller type.
   For B2C: consumer communities (r/expats, r/india, r/LifeAdvice, r/moving, r/FirstTimeHomeBuyer)
   For D2C: founder + buyer communities (r/ecommerce, r/Entrepreneur, r/BuyItForLife, r/frugal)
   For B2B: professional communities (r/marketing, r/SaaS, r/startups, r/sales)
   Each subreddit needs a search_query that surfaces INTENT POSTS (not generic discussion).

B. RSS feeds: 3-6 publicly accessible RSS feed URLs.
   For B2C: consumer portals, expat community feeds, classified site feeds, Google News consumer queries.
   For D2C: ProductHunt RSS, IndieHackers RSS, Google News for product category.
   For B2B: industry blogs, trade publications, Google News for company announcements.
   Format for Google News: https://news.google.com/rss/search?q=ENCODED_QUERY&hl=en

C. Serper discovery queries: 2-4 Google Search operator queries.
   For B2C: target WHERE consumers post wants (site:expatriates.com, site:dubizzle.com, site:reddit.com, site:quora.com)
     Examples: "site:expatriates.com Oman villa rent looking for"
               "site:reddit.com r/expats looking for apartment Muscat"
               "site:quora.com best interior designer Dubai recommend"
   For D2C: target product review and comparison sites.
     Examples: "site:reddit.com best alternatives to [product]"
               "site:quora.com recommend [product category]"
   For B2B: target professional forums and discussion boards.
     Examples: "site:community.hubspot.com marketing automation pain"

D. Geo filter terms: Geographic keywords to filter signals. Be specific (city names, not just country).

E. Buyer language context: 2-3 sentences on EXACT WORDS a high-intent buyer for this ICP uses.
   This is critical — it trains the inline scorer on what HIGH vs LOW looks like.

F. classified_listing_config (B2C/D2C ONLY):
   categories: What product/service terms appear in classified ads for this ICP
     (e.g. "villa", "apartment", "interior design", "moving service")
   platform_types: Which platform types apply — "property", "expat", "classified", "news"
   Leave EMPTY for B2B archetypes.

G. consumer_forum_config (B2C/D2C ONLY):
   product_category: Primary category for consumer forum search
   include_d2c_founders: true only for D2C (monitors IndieHackers, ProductHunt, Show HN)
   Leave EMPTY for B2B archetypes.

H. job_post_keywords (B2B/B2B2C ONLY): Role titles signaling a capability gap.
   Leave EMPTY for B2C/D2C archetypes.

IMPORTANT:
- Suggestions must match the specific ICP above — nothing generic
- Subreddit names must be REAL subreddits that exist
- RSS URLs must be properly formatted and publicly accessible
- For geo-specific ICPs: all sources must be geo-focused
- For B2C/D2C: fill classified_listing_config and consumer_forum_config
- For B2B: fill job_post_keywords and leave classified/consumer fields empty"""

        result = call_gemini_2_5(
            prompt,
            expect_json=True,
            response_schema=_ROUTING_SCHEMA,
        )
        if not isinstance(result, dict):
            raise ValueError(f"Routing schema returned unexpected type: {type(result)}")
        # Inject private routing hints for _instantiate_sources
        result["_archetype"]    = archetype
        result["_icp_context"]  = icp_context
        # V26: Pass campaign through for strategy-aware source instantiation
        result["_campaign"]     = campaign if campaign else {}
        return result

    def _instantiate_sources(
        self,
        config: dict,
        geo: str,
        campaign: dict | None = None,
    ) -> list[BaseSignalSource]:
        """Convert Gemini routing config into BaseSignalSource instances.

        Source instantiation order reflects priority:
        B2C/D2C: ClassifiedListings + ConsumerForum first (highest-intent consumer signals)
        B2B:     Reddit + HN + Job Posts first (professional community signals)
        All:     RSS + Serper discovery always last (broadest coverage, lowest specificity)

        Args:
            config:   Gemini routing config dict (with injected _archetype, _icp_context).
            geo:      Campaign location string.
            campaign: Full campaign document dict. Used for once-daily source cooldowns.
        """
        import datetime as _dt
        sources: list[BaseSignalSource] = []
        geo_filter_terms = config.get("geo_filter_terms", [])
        archetype = config.get("_archetype", "B2B").upper()
        is_consumer = "B2C" in archetype or "D2C" in archetype
        _campaign = campaign or {}

        # ------------------------------------------------------------------ #
        # V25.2.1 — Once-daily gate for expensive Serper sources.            #
        # Google Reviews uses 2 Serper credits per competitor (maps+reviews). #
        # At 5 competitors × 6 harvests/day × 150 campaigns = 45K Serper     #
        # calls/day just from reviews. Gate to once per 23h per campaign.    #
        # V26: COMPETITOR_TOUCHPOINT strategy reduces cooldown to 6h.        #
        # ------------------------------------------------------------------ #
        _intel_strategy_cfg = _campaign.get("intelligence_strategy") or {}
        _primary_strat = (
            _intel_strategy_cfg.get("primary", "")
            if isinstance(_intel_strategy_cfg, dict) else ""
        ).upper().strip()
        _REVIEWS_COOLDOWN_H = (
            6 if _primary_strat == "COMPETITOR_TOUCHPOINT" else 23
        )
        _reviews_due = True
        _last_reviews_raw = _campaign.get("last_google_reviews_at")
        if _last_reviews_raw:
            try:
                # Firestore DatetimeWithNanoseconds is tz-aware; handle both.
                if hasattr(_last_reviews_raw, "tzinfo"):
                    _last_reviews_ts = _last_reviews_raw
                    if _last_reviews_ts.tzinfo is None:
                        _last_reviews_ts = _last_reviews_ts.replace(
                            tzinfo=_dt.timezone.utc
                        )
                else:
                    # ISO string fallback
                    _last_reviews_ts = _dt.datetime.fromisoformat(
                        str(_last_reviews_raw)
                    ).replace(tzinfo=_dt.timezone.utc)
                _elapsed_h = (
                    _dt.datetime.now(_dt.timezone.utc) - _last_reviews_ts
                ).total_seconds() / 3600
                if _elapsed_h < _REVIEWS_COOLDOWN_H:
                    _reviews_due = False
                    log.info(
                        "source_router_google_reviews_cooldown_active",
                        campaign_id=_campaign.get("id", _campaign.get("campaign_id", "unknown")),
                        elapsed_hours=round(_elapsed_h, 1),
                        cooldown_hours=_REVIEWS_COOLDOWN_H,
                        strategy=_primary_strat or "DEFAULT",
                        note=f"Skipping GoogleReviewSource — ran less than {_REVIEWS_COOLDOWN_H}h ago. "
                             "Saves ~2 Serper credits per competitor per harvest.",
                    )
            except Exception as _cd_err:
                log.warning(
                    "source_router_google_reviews_cooldown_parse_failed",
                    error=str(_cd_err),
                    fallback="Treating as due — reviews will run this harvest.",
                )
                _reviews_due = True

        # === B2C / D2C primary sources ===================================

        # 1. Classified Listing Source (B2C/D2C first — highest-intent consumer signal)
        #    A consumer posting "Looking for 3BR villa Muscat" is an active buyer.
        classified_cfg = config.get("classified_listing_config", {})
        if classified_cfg and is_consumer:
            categories = [c for c in classified_cfg.get("categories", []) if c]
            platform_types = classified_cfg.get("platform_types", [])
            if categories:
                sources.append(ClassifiedListingSource(
                    categories     = categories,
                    geo            = geo,
                    platform_types = platform_types if platform_types else None,
                    max_age_days   = 14,
                ))
                log.info(
                    "source_router_classified_configured",
                    categories=categories,
                    geo=geo or "global",
                )

        # 2. Consumer Forum Source (B2C/D2C — product review + comparison signals)
        consumer_cfg = config.get("consumer_forum_config", {})
        if consumer_cfg and is_consumer:
            product_category = consumer_cfg.get("product_category", "").strip()
            include_founders = consumer_cfg.get("include_d2c_founders", False)
            if product_category:
                sources.append(ConsumerForumSource(
                    product_category     = product_category,
                    geo                  = geo,
                    include_d2c_founders = include_founders,
                    max_age_days         = 21,
                ))
                log.info(
                    "source_router_consumer_forum_configured",
                    product_category=product_category,
                    include_d2c_founders=include_founders,
                )

        # === Universal sources (all archetypes) ==========================

        # 3. Reddit (community discovery — archetype-adaptive)
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

        # 4. Hacker News (B2B, SaaS, startup, D2C founder only)
        hn_queries = [q for q in config.get("hackernews_queries", []) if q]
        if hn_queries:
            sources.append(HackerNewsSource(
                search_queries  = hn_queries,
                max_age_days    = 30,
                include_ask     = True,
                include_stories = True,
                min_comments    = 2,
            ))

        # 5. RSS feeds
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

        # 6. Job posts (B2B/B2B2C only — capability gap signal)
        job_keywords = [k for k in config.get("job_post_keywords", []) if k]
        if job_keywords and not is_consumer:
            sources.append(JobPostSource(
                role_keywords = job_keywords,
                geo           = geo,
                max_age_days  = 30,
            ))

        # 7. Serper discovery (URLs → PRISM scrapes for full content)
        #    For B2C/D2C: queries target consumer platforms (expatriates.com, dubizzle.com)
        #    For B2B: queries target professional forums and industry sites
        discovery_queries = [q for q in config.get("serper_discovery_queries", []) if q]
        if discovery_queries and self._serper_key:
            sources.append(SerperDiscoverySource(
                discovery_queries = discovery_queries,
                serper_api_key    = self._serper_key,
                num_results       = 10,
                geo_code          = "",  # Queries already contain geo-specificity
            ))

        # 8. Google Reviews (all archetypes — competitor review mining)
        #    Gemini derives competitor names from ICP; Serper Maps + Reviews
        #    fetch buyer-language reviews. Works for B2B service firms too.
        #    V25.2.1: Once-daily cooldown gate — _reviews_due is set above.
        if self._serper_key and _reviews_due:
            icp_context = config.get("_icp_context", "")
            raw_arch    = config.get("_archetype", "B2B")
            sources.append(GoogleReviewSource(
                icp_context    = icp_context,
                geo            = geo,
                archetype      = raw_arch,
                serper_api_key = self._serper_key,
                max_age_days   = 60,
            ))

        # 9. YouTube (B2C and D2C only — video discovery for consumer ICPs)
        #    B2C/D2C buyers research purchases on YouTube before converting.
        #    Uses search_queries from the routing config (same Gemini output).
        if is_consumer:
            yt_queries = [
                r.get("search_query", "")
                for r in config.get("reddit_sources", [])
                if r.get("search_query")
            ][:6]  # Derive from Reddit queries — they capture the same buyer intent
            if yt_queries:
                sources.append(YouTubeSource(
                    search_queries = yt_queries,
                    max_results    = 10,
                    max_age_days   = 30,
                ))
                log.info(
                    "source_router_youtube_configured",
                    archetype=raw_arch,
                    queries=len(yt_queries),
                )

        # ── V26 Strategy-aware source prioritization (Task 3.2) ───────────
        # Based on intelligence_strategy.primary, force-enable or force-disable
        # specific source types.
        _campaign_strat = config.get("_campaign", {}).get("intelligence_strategy") or {}
        _strat_primary = (
            _campaign_strat.get("primary", "")
            if isinstance(_campaign_strat, dict) else ""
        ).upper().strip()

        if _strat_primary:
            _existing_types = {s.source_type for s in sources}

            if _strat_primary == "PLATFORM_MINING":
                # Prioritize: SerperDiscovery + ClassifiedListings
                # Disable: Reddit, HackerNews
                sources = [
                    s for s in sources
                    if s.source_type not in ("reddit", "hackernews")
                ]
                # Force-add ClassifiedListings if not present
                if "classified_listings" not in _existing_types:
                    _platform_targets = _campaign_strat.get("platform_targets", [])
                    if _platform_targets:
                        sources.insert(0, ClassifiedListingSource(
                            categories=_platform_targets[:5],
                            geo=geo,
                            platform_types=["classified", "property", "expat"],
                            max_age_days=14,
                        ))
                log.info("source_router_strategy_override",
                         strategy=_strat_primary,
                         source_types=[s.source_type for s in sources])

            elif _strat_primary == "COMPETITOR_TOUCHPOINT":
                # Prioritize: GoogleReviews + SerperDiscovery
                # Disable: JobPosts
                sources = [
                    s for s in sources
                    if s.source_type != "job_posts"
                ]
                # Force-add GoogleReviewSource if not present and reviews are due
                if "google_reviews" not in _existing_types and self._serper_key and _reviews_due:
                    icp_ctx = config.get("_icp_context", "")
                    raw_a = config.get("_archetype", "B2B")
                    sources.insert(0, GoogleReviewSource(
                        icp_context=icp_ctx,
                        geo=geo,
                        archetype=raw_a,
                        serper_api_key=self._serper_key,
                        max_age_days=60,
                    ))
                log.info("source_router_strategy_override",
                         strategy=_strat_primary,
                         source_types=[s.source_type for s in sources])

            elif _strat_primary == "PROFESSIONAL_NETWORK":
                # Prioritize: SerperDiscovery (LinkedIn queries) + HackerNews
                # Disable: ClassifiedListings
                sources = [
                    s for s in sources
                    if s.source_type != "classified_listings"
                ]
                # Force-add HackerNewsSource if not present
                if "hackernews" not in _existing_types:
                    _decision_titles = _campaign_strat.get("decision_maker_titles", [])
                    _hn_queries = [
                        f'"{t}" evaluating OR implemented'
                        for t in (_decision_titles or ["CTO", "VP Engineering"])[:3]
                    ]
                    sources.append(HackerNewsSource(
                        search_queries=_hn_queries,
                        max_age_days=30,
                        include_ask=True,
                        include_stories=True,
                        min_comments=2,
                    ))
                log.info("source_router_strategy_override",
                         strategy=_strat_primary,
                         source_types=[s.source_type for s in sources])

            elif _strat_primary == "EVENT_TRIGGER_MINING":
                # Prioritize: RssFeed (news) + SerperDiscovery (news queries)
                # Disable: ClassifiedListings
                sources = [
                    s for s in sources
                    if s.source_type != "classified_listings"
                ]
                # Force-add RssFeedSource for news if not present
                if "rss_feed" not in _existing_types:
                    _event_types = _campaign_strat.get("event_types", ["funding", "expansion"])
                    from urllib.parse import quote_plus as _qp
                    _news_urls = [
                        f"https://news.google.com/rss/search?q={_qp(evt)}&hl=en"
                        for evt in (_event_types or ["funding"])[:3]
                    ]
                    sources.insert(0, RssFeedSource(
                        feed_urls=_news_urls,
                        max_age_days=7,
                    ))
                log.info("source_router_strategy_override",
                         strategy=_strat_primary,
                         source_types=[s.source_type for s in sources])

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

        is_consumer = "B2C" in norm_arch or "D2C" in norm_arch

        # Archetype → generic subreddit fallback set
        if "B2C" in norm_arch:
            subreddits = ["expats", "personalfinance", "travel", "LifeAdvice", "moving"]
        elif "D2C" in norm_arch:
            subreddits = ["Entrepreneur", "ecommerce", "smallbusiness", "BuyItForLife", "frugal"]
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

        # B2C fallback: ClassifiedListingSource with category terms from keywords
        if is_consumer and search_terms:
            sources.append(ClassifiedListingSource(
                categories     = search_terms[:3],
                geo            = geo,
                platform_types = ["expat", "news", "classified"],
                max_age_days   = 14,
            ))
            sources.append(ConsumerForumSource(
                product_category     = " ".join(search_terms[:2]),
                geo                  = geo,
                include_d2c_founders = "D2C" in norm_arch,
                max_age_days         = 21,
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
        if not is_consumer and ("B2B" in norm_arch or "SAAS" in norm_arch):
            sources.append(HackerNewsSource(
                search_queries  = search_terms[:2] if search_terms else ["startup problems"],
                max_age_days    = 30,
                include_ask     = True,
                include_stories = False,
                min_comments    = 3,
            ))

        return RoutingConfig(
            sources                = sources,
            buyer_language_context = "",
            geo_filter_terms       = [geo] if geo else [],
            archetype              = archetype,
            derived_by             = "fallback",
        )


# ---------------------------------------------------------------------------
# Module-level archetype instruction builder
# ---------------------------------------------------------------------------

def _build_archetype_instructions(archetype: str) -> str:
    """Return archetype-specific discovery instructions for the Gemini routing prompt.

    Each archetype has fundamentally different buyer behavior and therefore
    different optimal signal sources. These instructions ensure Gemini selects
    the right platform types, not just generic best practices.
    """
    arch = archetype.upper()

    if "B2B2C" in arch:
        return """ARCHETYPE: B2B2C (Business sells to Business that sells to Consumer)
Your ICP is a BUSINESS that needs to reach end consumers. Target:
- Companies looking for distribution partners, reseller networks, retail placement
- Retailers/wholesalers seeking new product lines to stock
- Franchise seekers or channel partner applicants

Signal sources priority:
1. Job posts: "Franchise Development Manager", "Channel Partner Manager", "Business Development" roles
2. Industry trade publication RSS feeds
3. Reddit: r/Entrepreneur, r/smallbusiness, r/FranchiseBusiness
4. Serper: site: queries on trade show sites, chamber of commerce forums
5. classified_listing_config: LEAVE EMPTY (this is B2B, not consumer)
6. consumer_forum_config: LEAVE EMPTY"""

    if "D2C" in arch:
        return """ARCHETYPE: D2C (Direct-to-Consumer)
Your ICP is EITHER a D2C founder needing services (supplier, logistics, creative, fintech)
OR a consumer buying directly from a brand. Determine from ICP context which it is.

IF targeting D2C FOUNDERS (buying services):
1. HN queries: Focus on Show HN, startup challenges, ecommerce pain points
2. Reddit: r/ecommerce, r/Entrepreneur, r/startups, r/smallbusiness
3. RSS: IndieHackers, ProductHunt, Shopify community
4. job_post_keywords: Roles like "Head of Growth", "Ecommerce Manager"
5. classified_listing_config: LEAVE EMPTY
6. consumer_forum_config: include_d2c_founders = true, product_category = the ICP's product space

IF targeting END CONSUMERS (buying D2C products):
1. Reddit: consumer intent subreddits (r/BuyItForLife, r/frugal, product-specific subs)
2. RSS: ProductHunt new launches, review blogs, Google News product comparisons
3. Serper: site:reddit.com "best [product]", site:quora.com "recommend [product]"
4. classified_listing_config: categories = product category terms
5. consumer_forum_config: product_category, include_d2c_founders = false
6. job_post_keywords: LEAVE EMPTY
7. HN queries: LEAVE EMPTY (consumers don't use HN)"""

    if "B2C" in arch:
        return """ARCHETYPE: B2C (Business sells to individual Consumer)
Your ICP is an INDIVIDUAL CONSUMER with a personal need. They do NOT post on LinkedIn
or Hacker News. They post on consumer communities, classified sites, expat forums, and
review platforms.

KEY INSIGHT: For B2C, Serper + PRISM is CRITICAL because consumers post on sites
without RSS (classified sites, expat forums). Serper finds the URL, PRISM reads the
full "Looking for..." post. This is the highest-quality B2C signal.

Signal sources priority:
1. classified_listing_config: ALWAYS fill this for B2C
   - categories: What the buyer would search for (not what you sell)
   - platform_types: "expat" if expat/international audience, "property" if real estate,
     "classified" for general goods/services, "news" always
2. Serper discovery: site: operators on WHERE CONSUMERS POST
   - site:expatriates.com for Middle East expat consumers
   - site:reddit.com r/{local community} for local intent
   - site:quora.com for service recommendations
   - site:dubizzle.com for UAE/Oman classified intent
3. Reddit: consumer communities (NOT professional ones)
   - r/expats for international relocation intent
   - r/{country specific} for local consumer intent
   - r/personalfinance, r/travel for context
4. RSS: Consumer portal feeds, local news, Google News with consumer queries
5. HN queries: LEAVE EMPTY (consumers don't use HN)
6. job_post_keywords: LEAVE EMPTY (irrelevant for B2C)
7. consumer_forum_config: Always fill this
   - product_category: Main category the consumer is buying
   - include_d2c_founders: false"""

    # Default: B2B
    return """ARCHETYPE: B2B (Business sells to Business)
Your ICP is a PROFESSIONAL DECISION MAKER or BUSINESS. They post on LinkedIn,
professional Reddit communities, Hacker News (if tech), and industry forums.

Signal sources priority:
1. Reddit: Professional communities (r/marketing, r/SaaS, r/startups, r/sales, etc.)
   - Search queries must surface PAIN POSTS not generic discussion
   - Examples: "we tried X and it failed", "looking for alternative to", "anyone dealt with"
2. Hacker News: Use for tech/SaaS B2B ICPs
   - Ask HN posts are highest quality: "Ask HN: How do you handle X?"
3. Job Posts: ALWAYS include for B2B — capability gaps = vendor opportunities
   - Think: what role does the buyer hire when they DON'T have your solution?
4. RSS: Industry trade publications, company blog RSS, Google News for company triggers
   - Company triggers: funding rounds, new executive hires, rebrands, expansions
5. Serper: Professional forums, community boards, Slack community sites
   - site:community.hubspot.com, site:indiehackers.com, site:reddit.com
6. classified_listing_config: LEAVE EMPTY (B2B buyers don't post on classified sites)
7. consumer_forum_config: LEAVE EMPTY"""


# ---------------------------------------------------------------------------
# V26 Strategy-specific instruction builder
# ---------------------------------------------------------------------------

def _build_strategy_instructions(strategy: str, campaign: dict) -> str:
    """Return intelligence-strategy-specific Gemini instructions.

    These instructions are PREPENDED to the archetype instructions, overriding
    default source selection priorities when the campaign has an explicit
    intelligence strategy configured.

    Args:
        strategy: Primary intelligence strategy string (e.g. 'PLATFORM_MINING').
        campaign: Full campaign dict for extracting strategy parameters.

    Returns:
        Strategy instruction string, or empty string if strategy is unknown.
    """
    _strat = (strategy or "").upper().strip()
    _intel = campaign.get("intelligence_strategy") or {}
    if not isinstance(_intel, dict):
        _intel = {}

    if _strat == "PLATFORM_MINING":
        _platform_targets = _intel.get("platform_targets", [])
        _targets_str = ", ".join(_platform_targets) if _platform_targets else "(none specified)"
        return f"""--- INTELLIGENCE STRATEGY: PLATFORM MINING ---
PRIMARY OBJECTIVE: Find pages on competitor/aggregator platforms that list individual entities (agents, companies, profiles, reviewers).
TARGET PLATFORMS: {_targets_str}
Generate queries that search INSIDE these platforms for entity profiles, contact info, and listings.
Examples: site:dubizzle.com.om "agent" "villa" "contact"
          site:g2.com/products/*/reviews "hospital" "implemented"
Do NOT generate generic forum queries — platform mining is the priority."""

    if _strat == "COLLOQUIAL_DISCOVERY":
        _vocab_notes = _intel.get("vocabulary_notes", "")
        return f"""--- INTELLIGENCE STRATEGY: COLLOQUIAL DISCOVERY ---
VOCABULARY RULE: {_vocab_notes}
All queries must use EVERYDAY LANGUAGE of the target buyer, NOT industry jargon.
The buyer does NOT use professional terms. Think about how a real person would type their frustration into Google."""

    if _strat == "COMPETITOR_TOUCHPOINT":
        _competitor_names = _intel.get("competitor_names", [])
        _names_str = ", ".join(_competitor_names) if _competitor_names else "(none specified)"
        return f"""--- INTELLIGENCE STRATEGY: COMPETITOR TOUCHPOINT ---
PRIMARY OBJECTIVE: Find reviews, comments, and public engagement with these competitors: {_names_str}
Prioritize: Google Reviews, G2/Capterra reviews, YouTube comments, public social posts.
The REVIEWER/COMMENTER is the lead — not the competitor page itself."""

    if _strat == "PROFESSIONAL_NETWORK":
        _decision_titles = _intel.get("decision_maker_titles", [])
        _titles_str = ", ".join(_decision_titles) if _decision_titles else "(none specified)"
        return f"""--- INTELLIGENCE STRATEGY: PROFESSIONAL NETWORK ---
PRIMARY OBJECTIVE: Find decision-maker posts on LinkedIn about evaluation and purchase decisions.
Target titles: {_titles_str}
Generate LinkedIn-focused queries: site:linkedin.com/posts "evaluating" OR "implemented" OR "RFP"
Do NOT exclude LinkedIn — it is the PRIMARY intelligence surface."""

    if _strat == "EVENT_TRIGGER_MINING":
        _event_types = _intel.get("event_types", [])
        _events_str = ", ".join(_event_types) if _event_types else "(none specified)"
        return f"""--- INTELLIGENCE STRATEGY: EVENT TRIGGER MINING ---
PRIMARY OBJECTIVE: Find NEWS EVENTS that signal purchase urgency.
Event types: {_events_str}
Generate Google News queries, regulatory filing queries, and announcement queries.
The event ITSELF qualifies the lead — no forum posts needed."""

    return ""
