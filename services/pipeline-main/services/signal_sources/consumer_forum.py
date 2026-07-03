"""
Consumer Forum Source — V25.1.0
================================
Targets consumer review and product discovery platforms for D2C and B2C ICPs.

Unlike Reddit (which is discussion-heavy) or classified sites (want ads),
consumer forums contain buyers who are EVALUATING, COMPARING, or SWITCHING
products — the peak conversion moment for D2C brands.

Signal categories:
  1. PRODUCT COMPARISONS — "Product X vs Product Y, which is better?"
  2. PURCHASE INTENT — "Looking for best X under price Y"
  3. FRUSTRATION + SWITCH — "Stopped using X, what alternatives exist?"
  4. RECOMMENDATION REQUEST — "Anyone using Y? Worth it?"

Platforms covered:
  - Reddit product communities (r/BuyItForLife, r/frugal, r/ProductReviews)
  - ProductHunt discussions (via RSS)
  - Trustpilot / G2 trending reviews (via Google News RSS)
  - Quora topics (via Google News RSS on Quora answers)
  - YouTube community posts (via Google News RSS for high-intent searches)
  - Consumer reports / review blogs (via Google News RSS)
  - Amazon review discussions (via Google News RSS)
  - IndieHackers product launches + discussions (via RSS)

D2C Founder signals (a different ICP altogether):
  D2C founders are buyers of: creative services, manufacturing, logistics,
  fulfillment, fintech, marketing platforms, influencer platforms.
  For D2C founder ICP, use the HackerNews source (Show HN) and IndieHackers RSS.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus

from core.logging import get_logger                                    # type: ignore[import]
from services.signal_sources.base import BaseSignalSource, SignalItem  # type: ignore[import]
from services.signal_sources.rss_feed import RssFeedSource             # type: ignore[import]
from services.signal_sources.reddit import RedditSource                # type: ignore[import]

log = get_logger("pipeline.signal_sources.consumer_forum")


class ConsumerForumSource(BaseSignalSource):
    """Intent signal discovery from consumer review and D2C community platforms.

    Targets the moment when a consumer is actively evaluating, comparing,
    or switching products — the highest-value conversion window.

    Args:
        product_category: The product or service category (e.g. "skincare",
                          "running shoes", "meal kit delivery", "accounting software").
        target_persona:   Optional buyer persona description (e.g. "young professionals",
                          "new parents", "home cooks"). Used to select communities.
        geo:              Geographic focus. Empty = global.
        include_d2c_founders: If True, also monitor founder communities (Indie Hackers,
                              r/ecommerce, Show HN) for D2C service provider ICPs.
        max_age_days:     Signal age cutoff.
        max_per_source:   Max signals per discover() call.
    """

    source_type = "consumer_forum"

    def __init__(
        self,
        product_category: str,
        target_persona: str = "",
        geo: str = "",
        include_d2c_founders: bool = False,
        max_age_days: int = 21,
        max_per_source: int = 40,
    ) -> None:
        self._product_category     = product_category.lower().strip()
        self._target_persona       = target_persona.lower().strip()
        self._geo                  = geo.strip()
        self._include_d2c_founders = include_d2c_founders
        self._max_age_days         = max_age_days
        self._max_per_source       = max_per_source

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def discover(self) -> list[SignalItem]:
        """Collect consumer forum signals for the product category."""
        seen_urls: set[str] = set()
        signals:   list[SignalItem] = []

        # 1. Product-specific Reddit communities (consumer intent subreddits)
        reddit_signals = self._fetch_reddit()
        for sig in reddit_signals:
            if sig.url not in seen_urls:
                seen_urls.add(sig.url)
                signals.append(sig)

        # 2. Review and comparison platform RSS (via Google News)
        rss_signals = self._fetch_consumer_rss()
        for sig in rss_signals:
            if sig.url not in seen_urls:
                seen_urls.add(sig.url)
                signals.append(sig)

        # 3. D2C founder signals (Show HN, IndieHackers) — when relevant
        if self._include_d2c_founders:
            founder_signals = self._fetch_founder_rss()
            for sig in founder_signals:
                if sig.url not in seen_urls:
                    seen_urls.add(sig.url)
                    signals.append(sig)

        log.info(
            "consumer_forum_discover_complete",
            product_category=self._product_category,
            geo=self._geo or "global",
            signals_found=len(signals),
        )
        return signals[: self._max_per_source]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_reddit(self) -> list[SignalItem]:
        """Fetch from consumer-intent Reddit communities."""
        # Universal high-intent consumer subreddits (always relevant for D2C/B2C)
        _UNIVERSAL_CONSUMER = [
            "BuyItForLife",      # "What's the best X?"
            "frugal",            # Price-conscious buyers
            "PersonalFinance",   # Purchase decision discussion
            "AmazonReviews",     # Direct product comparison
        ]

        # Derive category-specific subreddits from product category
        # These are discovered heuristically; source_router's Gemini call
        # will select the precise ones for each campaign
        category_words = self._product_category.split()
        search_queries = [
            f"looking for {self._product_category} recommendation",
            f"best {self._product_category} alternative",
            f"anyone use {self._product_category}",
        ]
        if self._geo:
            search_queries.append(f"{self._product_category} {self._geo}")

        geo_terms = [self._geo.lower()] if self._geo else None

        try:
            src = RedditSource(
                subreddits   = _UNIVERSAL_CONSUMER,
                search_terms = search_queries[:3],
                geo_terms    = geo_terms,
                max_age_days = self._max_age_days,
                max_per_source = 20,
            )
            raw = src.discover()
            # Re-tag to consumer_forum
            return [
                SignalItem(
                    url         = s.url,
                    text        = s.text,
                    title       = s.title,
                    author      = s.author,
                    source_type = self.source_type,
                    fetched_at  = s.fetched_at or self._now_iso(),
                    metadata    = {**s.metadata, "consumer_forum_type": "reddit_consumer"},
                )
                for s in raw
            ]
        except Exception as exc:
            log.warning("consumer_forum_reddit_failed", error=str(exc))
            return []

    def _fetch_consumer_rss(self) -> list[SignalItem]:
        """Fetch from consumer review platforms via Google News RSS."""
        geo_suffix = f" {self._geo}" if self._geo else ""
        cat = self._product_category

        # Intent-rich queries targeting review platforms
        queries = [
            f'"{cat}" recommendation OR "best {cat}" OR "{cat} alternatives"',
            f'site:reddit.com "{cat}" looking for OR "recommend" OR "switch"',
            f'site:quora.com "{cat}" recommend OR "which is better"',
            f'"{cat}" review 2024 OR 2025{geo_suffix}',
        ]

        feed_urls = [
            f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en"
            for q in queries[:4]
        ]

        try:
            rss = RssFeedSource(
                feed_urls       = feed_urls,
                keyword_filters = [cat] + category_words_split(cat),
                max_age_days    = self._max_age_days,
                max_per_source  = 25,
            )
            raw = rss.discover()
            return [
                SignalItem(
                    url         = s.url,
                    text        = s.text,
                    title       = s.title,
                    author      = s.author,
                    source_type = self.source_type,
                    fetched_at  = s.fetched_at or self._now_iso(),
                    metadata    = {**s.metadata, "consumer_forum_type": "review_platform", "is_thin_content": True},
                )
                for s in raw
            ]
        except Exception as exc:
            log.warning("consumer_forum_rss_failed", error=str(exc))
            return []

    def _fetch_founder_rss(self) -> list[SignalItem]:
        """Fetch D2C founder signals from Indie Hackers and product communities."""
        cat = self._product_category

        founder_feeds = [
            # Indie Hackers — D2C founders sharing challenges
            "https://www.indiehackers.com/feed.xml",
            # ProductHunt — new D2C products (competitor + buyer signal)
            "https://www.producthunt.com/feed",
            # Google News — D2C founder content
            f"https://news.google.com/rss/search?q={quote_plus(f'direct to consumer {cat} founder startup')}&hl=en",
        ]

        try:
            rss = RssFeedSource(
                feed_urls       = founder_feeds,
                keyword_filters = [cat] + category_words_split(cat),
                max_age_days    = 30,
                max_per_source  = 15,
            )
            raw = rss.discover()
            return [
                SignalItem(
                    url         = s.url,
                    text        = s.text,
                    title       = s.title,
                    author      = s.author,
                    source_type = self.source_type,
                    fetched_at  = s.fetched_at or self._now_iso(),
                    metadata    = {**s.metadata, "consumer_forum_type": "d2c_founder", "is_thin_content": True},
                )
                for s in raw
            ]
        except Exception as exc:
            log.warning("consumer_forum_founder_rss_failed", error=str(exc))
            return []


def category_words_split(category: str) -> list[str]:
    """Split category into meaningful words for keyword filtering."""
    return [w for w in category.split() if len(w) > 3]
